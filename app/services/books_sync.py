"""Mirror the vouchers Tally accepted into HNC Books, the accounting-only app.

Books keeps no stock, so each frozen Tally request is rewritten as a plain ledger
voucher: the sales/purchase amounts that Tally carries inside inventory lines
become ledger lines, and every ledger used is sent as a master so Books can
create the ones it lacks (existing Books ledgers are left as they are). Books
has no API, so Master logs in like a browser and uses its Tally XML import.
The voucher GUID is fixed per batch and Books skips a GUID it already holds,
which makes every retry safe.

Two kinds of failure are kept apart. When Books is down or refuses the login,
nothing is recorded on the batches and the next run tries again. When Books
answers but refuses a voucher, the reason is stored on that batch, and only the
manual "Send waiting vouchers now" tries it again, so a voucher Books will never
take neither blocks newer ones nor fills Books' audit log every few minutes.
"""

from __future__ import annotations

import logging
import re
import uuid
import xml.etree.ElementTree as ET  # nosec B405 - builds requests only; input parsed with defusedxml
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from http.client import HTTPException
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Batch, BatchStatus, BatchType, utc_now
from app.services.settings import get_all_settings

logger = logging.getLogger("setuora")

BOOKS_TIMEOUT_SECONDS = 30
MAX_BOOKS_RESPONSE_BYTES = 2 * 1024 * 1024
# Books cuts ledger names to this length when it creates a ledger.
BOOKS_LEDGER_NAME_LIMIT = 120
NOTHING_TO_POST = "Nothing to post to Books: every amount is zero."
BOOKS_VOUCHER_TYPES = {
    BatchType.SALE.value: "Sales",
    BatchType.SALES_RETURN.value: "Credit Note",
    BatchType.PURCHASE.value: "Purchase",
    BatchType.RECEIVE.value: "Purchase",
}
_RESULT = re.compile(r"vouchers (\d+), skipped (\d+)")
# Books answers its import with an HTML page; these match app/main.py tally_import there.
_ERROR_LIST = re.compile(r"<ul class=bad>(.*?)</ul>", re.S)
_ERROR_ITEM = re.compile(r"<li>(.*?)</li>", re.S)
_FAILURE = re.compile(r"<p class=bad>(.*?)</p>", re.S)
_TAGS = re.compile(r"<[^>]+>")


class BooksError(RuntimeError):
    """Books answered and refused this voucher."""


class BooksUnavailable(BooksError):
    """Books could not be used at all (down, login refused): no voucher is to blame."""


@dataclass(frozen=True)
class BooksConfig:
    url: str
    username: str
    password: str
    mirror_from: date | None = None


def books_config() -> BooksConfig | None:
    settings = get_settings()
    if not (settings.books_url and settings.books_username and settings.books_password):
        return None
    try:
        mirror_from = (
            date.fromisoformat(settings.books_mirror_from) if settings.books_mirror_from else None
        )
    except ValueError:
        raise BooksUnavailable("BOOKS_MIRROR_FROM must be a date like 2026-12-01.") from None
    return BooksConfig(
        settings.books_url, settings.books_username, settings.books_password, mirror_from
    )


def books_guid(batch: Batch) -> str:
    return f"SETUORA-{batch.sync_remote_id or batch.batch_number}"


def _text(parent: ET.Element, tag: str, value: str) -> None:
    ET.SubElement(parent, tag).text = value


def _is_zero(amount: str) -> bool:
    try:
        return Decimal(amount) == 0
    except InvalidOperation:
        raise BooksError(f"Tally amount {amount!r} is not a number.") from None


def build_books_xml(batch: Batch, round_off_ledger: str = "") -> str | None:
    """Rewrite the request Tally accepted as a Books ledger voucher; None if nothing to post."""
    voucher_type = BOOKS_VOUCHER_TYPES.get(batch.batch_type)
    if voucher_type is None or not batch.sync_request_xml:
        raise BooksError(f"No Tally voucher to mirror for {batch.batch_type}")
    try:
        root = safe_fromstring(batch.sync_request_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise BooksError(f"The Tally request for this batch is unreadable: {exc}") from exc
    tally_voucher = next((node for node in root.iter() if node.tag == "VOUCHER"), None)
    if tally_voucher is None:
        raise BooksError("The Tally request for this batch has no voucher.")
    is_sales_side = batch.batch_type in {BatchType.SALE.value, BatchType.SALES_RETURN.value}
    # Requests frozen before ISPARTYLEDGER was written still name the party here.
    party = tally_voucher.findtext("PARTYLEDGERNAME", "").strip()
    groups = {
        "party": "Sundry Debtors" if is_sales_side else "Sundry Creditors",
        "item": "Sales Accounts" if is_sales_side else "Purchase Accounts",
        "tax": "Duties & Taxes",
    }

    lines: list[tuple[str, str, str]] = []  # ledger, Tally-signed amount, Books group

    def add(entry: ET.Element, group: str) -> None:
        amount = entry.findtext("AMOUNT", "0").strip()
        # Books refuses a voucher with any zero line; dropping it keeps the balance.
        if not _is_zero(amount):
            name = entry.findtext("LEDGERNAME", "").strip()[:BOOKS_LEDGER_NAME_LIMIT]
            lines.append((name, amount, group))

    for child in tally_voucher:
        if child.tag == "LEDGERENTRIES.LIST":
            name = child.findtext("LEDGERNAME", "").strip()
            if child.findtext("ISPARTYLEDGER", "") == "Yes" or (party and name == party):
                add(child, groups["party"])
            elif name.casefold() == round_off_ledger.strip().casefold():
                add(child, "Indirect Expenses")
            else:
                add(child, groups["tax"])
        elif child.tag == "ALLINVENTORYENTRIES.LIST":
            for allocation in child.findall("ACCOUNTINGALLOCATIONS.LIST"):
                add(allocation, groups["item"])
    if len(lines) < 2:
        return None

    envelope = ET.Element("ENVELOPE")
    _text(ET.SubElement(envelope, "HEADER"), "TALLYREQUEST", "Import Data")
    request_data = ET.SubElement(
        ET.SubElement(ET.SubElement(envelope, "BODY"), "IMPORTDATA"), "REQUESTDATA"
    )
    seen: set[str] = set()
    for name, _amount, group in lines:
        if name in seen:
            continue
        seen.add(name)
        ledger = ET.SubElement(
            ET.SubElement(request_data, "TALLYMESSAGE"), "LEDGER", {"NAME": name}
        )
        _text(ledger, "NAME", name)
        _text(ledger, "PARENT", group)

    voucher = ET.SubElement(
        ET.SubElement(request_data, "TALLYMESSAGE"), "VOUCHER", {"VCHTYPE": voucher_type}
    )
    _text(voucher, "DATE", tally_voucher.findtext("DATE", ""))
    _text(voucher, "GUID", books_guid(batch))
    _text(voucher, "VOUCHERTYPENAME", voucher_type)
    _text(voucher, "VOUCHERNUMBER", tally_voucher.findtext("VOUCHERNUMBER", batch.batch_number))
    tally_type = tally_voucher.findtext("VOUCHERTYPENAME", "")
    narration = tally_voucher.findtext("NARRATION", "")
    # Books' own "Final export to Tally" would post it into Tally a second time.
    _text(
        voucher,
        "NARRATION",
        f"{narration} (Tally voucher type: {tally_type}). "
        "Already in Tally: leave out of Books' Final export to Tally.",
    )
    for name, amount, _group in lines:
        entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        _text(entry, "LEDGERNAME", name)
        _text(entry, "AMOUNT", amount)
    return ET.tostring(envelope, encoding="unicode")


class BooksClient:
    """One logged-in Books session, reused for every voucher in a run."""

    def __init__(self, config: BooksConfig):
        self.base = config.url.rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            raise BooksUnavailable("BOOKS_URL must start with http:// or https://")
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        path = self._open(
            Request(  # noqa: S310 - scheme checked above
                f"{self.base}/login",
                data=urlencode({"username": config.username, "password": config.password}).encode(),
                method="POST",
            )
        )[0]
        self.csrf = next((c.value for c in self.jar if c.name == "books_csrf"), "")
        if not self.csrf:
            raise BooksUnavailable(
                "Books login failed: check BOOKS_USERNAME and BOOKS_PASSWORD "
                "(Books also pauses a login after too many wrong passwords)."
            )
        if path == "/password":
            raise BooksUnavailable(
                "Books wants this login's password changed: log in to Books once as that user, "
                "change it, and update BOOKS_PASSWORD."
            )

    def _open(self, request: Request) -> tuple[str, str]:
        """Return (final path after redirects, page)."""
        try:
            # BooksClient fixes the scheme to http(s) before any request.
            with self.opener.open(request, timeout=BOOKS_TIMEOUT_SECONDS) as response:  # nosec B310
                page = response.read(MAX_BOOKS_RESPONSE_BYTES).decode("utf-8", errors="replace")
                return urlsplit(response.geturl()).path, page
        except HTTPError as exc:
            raise BooksUnavailable(f"Books refused the request (HTTP {exc.code}).") from exc
        except (URLError, OSError, HTTPException) as exc:
            raise BooksUnavailable(f"Books is not reachable: {exc}") from exc

    def import_voucher(self, xml: str) -> str:
        """Send one voucher; return 'posted' or 'already in Books', or raise BooksError."""
        boundary = uuid.uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="setuora.xml"\r\n'
                "Content-Type: text/xml\r\n\r\n"
            ).encode()
            + xml.encode()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        path, page = self._open(
            Request(  # noqa: S310 - scheme checked in __init__
                f"{self.base}/tally/import",
                data=body,
                method="POST",
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "X-CSRF-Token": self.csrf,
                },
            )
        )
        if path in {"/login", "/password", "/setup"}:
            raise BooksUnavailable(
                f"Books sent Master to its {path} page: the session was refused."
            )
        match = _RESULT.search(page)
        error_list = _ERROR_LIST.search(page)
        errors = (
            [unescape(_TAGS.sub("", e)) for e in _ERROR_ITEM.findall(error_list.group(1))]
            if error_list
            else []
        )
        if match and match.group(1) == "1":
            return "posted"
        if match and match.group(2) == "1" and not errors:
            return "already in Books"
        failure = _FAILURE.search(page)
        detail = "; ".join(errors) or (
            unescape(_TAGS.sub("", failure.group(1))).strip() if failure else ""
        )
        raise BooksError(f"Books did not take the voucher: {detail or 'unexpected reply'}")


def mirror_to_books(limit: int = 20, *, retry_rejected: bool = False) -> int:
    """Post vouchers Tally accepted that Books has not confirmed yet; return how many posted.

    Vouchers Books refused before are included only with retry_rejected (the manual
    push). Raises BooksUnavailable when Books cannot be used; no batch is marked then.
    """
    config = books_config()
    if config is None:
        return 0
    with SessionLocal() as db:
        query = select(Batch).where(
            Batch.status == BatchStatus.SYNCED.value,
            Batch.books_synced_at.is_(None),
            Batch.sync_request_xml.is_not(None),
            Batch.batch_type.in_(BOOKS_VOUCHER_TYPES),
        )
        if config.mirror_from:
            query = query.where(
                Batch.synced_at >= datetime.combine(config.mirror_from, datetime.min.time(), UTC)
            )
        if not retry_rejected:
            query = query.where(Batch.books_error.is_(None))
        batches = db.scalars(
            # Never-tried vouchers first, so old refusals cannot hold up new ones; then the
            # refusals tried longest ago, so a manual push works through all of them in turn.
            query.order_by(
                Batch.books_error.is_not(None), Batch.books_tried_at, Batch.synced_at, Batch.id
            ).limit(limit)
        ).all()
        if not batches:
            return 0
        round_off = get_all_settings(db).get("round_off_ledger_name", "")
        client = BooksClient(config)
        posted = 0
        for batch in batches:
            batch.books_tried_at = utc_now()
            try:
                xml = build_books_xml(batch, round_off)
                outcome = client.import_voucher(xml) if xml else "nothing to post"
            except BooksUnavailable:
                raise
            except BooksError as exc:
                batch.books_error = str(exc)[:1000]
                db.commit()
                continue
            logger.info("Books mirror %s: %s", batch.batch_number, outcome)
            batch.books_synced_at = utc_now()
            batch.books_error = None if xml else NOTHING_TO_POST
            db.commit()
            posted += 1 if xml else 0
        return posted
