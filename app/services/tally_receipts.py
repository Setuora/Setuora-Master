"""Read a franchise's receipt vouchers from central Tally and hand them to its Lite.

Each franchise can name a Tally receipt voucher type (for example
"Recipets Chintamani Warehouse"). Sync reads those vouchers for a date range,
stores each one once by Tally GUID, and queues a TALLY_RECEIPTS command with
the new or changed receipts and their customer ledgers. Lite bills its sales
against those ledgers, and the sales come back to Tally under the franchise's
own sales voucher type.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET  # nosec B405 - builds requests only; responses use defusedxml
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import FranchiseNode, TallyReceipt, utc_now
from app.services.network_ingest import TALLY_RECEIPTS_COMMAND, queue_node_command
from app.services.settings import get_setting, update_settings
from app.services.tally_masters import (
    TallyDataError,
    _build_collection_export,
    _direct_text,
    _local_tag,
    _post_read_request,
)
from app.services.tally_products import active_tally_source

SYNC_MINUTES_KEY = "tally_receipt_sync_minutes"
SYNC_DAYS_KEY = "tally_receipt_sync_days"
SYNC_LAST_AT_KEY = "tally_receipt_sync_last_at"
SYNC_LAST_RESULT_KEY = "tally_receipt_sync_last_result"
DEFAULT_SYNC_MINUTES = 0
DEFAULT_SYNC_DAYS = 60
MAX_SYNC_MINUTES = 1440
MAX_SYNC_DAYS = 731
RECEIPT_READ_TIMEOUT_SECONDS = 60
# Keep each queued command well under the 4 MB command poll budget.
RECEIPTS_PER_COMMAND = 200


@dataclass(frozen=True)
class ReceiptLine:
    ledger_name: str
    amount: str

    def as_dict(self) -> dict[str, str]:
        return {"ledger_name": self.ledger_name, "amount": self.amount}


@dataclass(frozen=True)
class ParsedReceipt:
    guid: str
    master_id: str
    alter_id: str
    voucher_type: str
    voucher_number: str
    voucher_date: date
    bank_ledger: str
    total_amount: Decimal
    narration: str
    lines: tuple[ReceiptLine, ...]


@dataclass
class ReceiptSyncSummary:
    franchise_code: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    command_ids: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.franchise_code}: read {self.fetched} receipts from Tally, "
            f"{self.created} new, {self.updated} changed, {self.unchanged} unchanged."
        )


def _validate_voucher_type(voucher_type: str) -> str:
    value = voucher_type.strip()
    if not value:
        raise TallyDataError("Set this franchise's Tally receipt voucher type first.")
    if '"' in value or len(value) > 80:
        raise TallyDataError("The Tally receipt voucher type name is not valid.")
    return value


def build_receipt_vouchers_xml(
    company_name: str, voucher_type: str, from_date: date, to_date: date
) -> str:
    envelope, tdl_message = _build_collection_export(
        "Setuora Franchise Receipts",
        "Voucher",
        (
            "Date",
            "VoucherNumber",
            "VoucherTypeName",
            "PartyLedgerName",
            "Narration",
            "GUID",
            "MasterID",
            "AlterID",
            "AllLedgerEntries",
        ),
        company_name=company_name.strip(),
    )
    static_variables = envelope.find("./BODY/DESC/STATICVARIABLES")
    if static_variables is None:  # pragma: no cover - constructed immediately above
        raise RuntimeError("Tally request is missing static variables")
    ET.SubElement(static_variables, "SVFROMDATE", {"TYPE": "Date"}).text = from_date.strftime(
        "%Y%m%d"
    )
    ET.SubElement(static_variables, "SVTODATE", {"TYPE": "Date"}).text = to_date.strftime("%Y%m%d")
    collection = next(node for node in tdl_message if _local_tag(node) == "COLLECTION")
    ET.SubElement(collection, "FILTER").text = "SetuoraFranchiseReceiptFilter"
    formula = ET.SubElement(
        tdl_message,
        "SYSTEM",
        {"TYPE": "Formulae", "NAME": "SetuoraFranchiseReceiptFilter"},
    )
    formula.text = (
        f'$VoucherTypeName = "{_validate_voucher_type(voucher_type)}" '
        "AND $Date >= ##SVFromDate AND $Date <= ##SVToDate"
    )
    return ET.tostring(envelope, encoding="unicode")


def _amount(raw: str) -> Decimal | None:
    cleaned = raw.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _tally_date(raw: str) -> date | None:
    value = raw.strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def parse_receipt_vouchers(root: ET.Element, voucher_type: str) -> list[ParsedReceipt]:
    receipts: list[ParsedReceipt] = []
    seen: set[str] = set()
    for voucher in root.iter():
        if _local_tag(voucher) != "VOUCHER":
            continue
        guid = _direct_text(voucher, "GUID")
        voucher_date = _tally_date(_direct_text(voucher, "DATE"))
        if not guid or voucher_date is None or guid in seen:
            continue
        lines: list[ReceiptLine] = []
        bank_ledger = ""
        total = Decimal("0")
        for entry in voucher:
            if _local_tag(entry) not in {"ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"}:
                continue
            ledger = _direct_text(entry, "LEDGERNAME")
            amount = _amount(_direct_text(entry, "AMOUNT"))
            if not ledger or amount is None or amount == 0:
                continue
            # Tally gives credits as positive amounts: the customers who paid.
            if amount > 0:
                lines.append(ReceiptLine(ledger, format(amount, "f")))
                total += amount
            elif not bank_ledger:
                bank_ledger = ledger
        if not lines:
            continue
        seen.add(guid)
        receipts.append(
            ParsedReceipt(
                guid=guid,
                master_id=_direct_text(voucher, "MASTERID"),
                alter_id=_direct_text(voucher, "ALTERID"),
                voucher_type=_direct_text(voucher, "VOUCHERTYPENAME") or voucher_type,
                voucher_number=_direct_text(voucher, "VOUCHERNUMBER"),
                voucher_date=voucher_date,
                bank_ledger=bank_ledger,
                total_amount=total,
                narration=_direct_text(voucher, "NARRATION"),
                lines=tuple(lines),
            )
        )
    return sorted(receipts, key=lambda item: (item.voucher_date, item.voucher_number, item.guid))


def fetch_franchise_receipts(
    config: dict[str, str],
    company_name: str,
    voucher_type: str,
    from_date: date,
    to_date: date,
) -> list[ParsedReceipt]:
    if from_date > to_date:
        raise TallyDataError("The start date must be on or before the end date.")
    xml = build_receipt_vouchers_xml(company_name, voucher_type, from_date, to_date)
    _, root = _post_read_request(config, xml, timeout=RECEIPT_READ_TIMEOUT_SECONDS)
    return parse_receipt_vouchers(root, voucher_type)


def _command_receipt(receipt: TallyReceipt) -> dict[str, object]:
    return {
        "tally_guid": receipt.tally_guid,
        "voucher_type": receipt.voucher_type,
        "voucher_number": receipt.voucher_number,
        "voucher_date": receipt.voucher_date.isoformat(),
        "bank_ledger": receipt.bank_ledger,
        "total_amount": format(Decimal(str(receipt.total_amount)), "f"),
        "narration": receipt.narration,
        "lines": receipt.lines,
    }


def apply_franchise_receipts(
    db: Session, node: FranchiseNode, parsed: list[ParsedReceipt]
) -> ReceiptSyncSummary:
    """Store receipts idempotently and queue the new or changed ones for Lite."""
    summary = ReceiptSyncSummary(franchise_code=node.code, fetched=len(parsed))
    existing = {
        row.tally_guid: row
        for row in db.scalars(
            select(TallyReceipt).where(TallyReceipt.tally_guid.in_([item.guid for item in parsed]))
        )
    }
    changed: list[TallyReceipt] = []
    now = utc_now()
    for item in parsed:
        lines_json = json.dumps([line.as_dict() for line in item.lines], ensure_ascii=False)
        row = existing.get(item.guid)
        if row is not None and row.franchise_id != node.id:
            # The same Tally voucher already belongs to another franchise.
            summary.unchanged += 1
            continue
        if row is None:
            row = TallyReceipt(franchise_id=node.id, tally_guid=item.guid, first_synced_at=now)
            db.add(row)
            summary.created += 1
        elif (
            row.tally_alter_id == item.alter_id
            and row.lines_json == lines_json
            and row.voucher_date == item.voucher_date
        ):
            row.last_synced_at = now
            summary.unchanged += 1
            continue
        else:
            summary.updated += 1
        row.tally_master_id = item.master_id or None
        row.tally_alter_id = item.alter_id or None
        row.voucher_type = item.voucher_type
        row.voucher_number = item.voucher_number or None
        row.voucher_date = item.voucher_date
        row.bank_ledger = item.bank_ledger or None
        row.total_amount = float(item.total_amount)
        row.narration = item.narration or None
        row.lines_json = lines_json
        row.last_synced_at = now
        changed.append(row)
    db.flush()
    for start in range(0, len(changed), RECEIPTS_PER_COMMAND):
        chunk = changed[start : start + RECEIPTS_PER_COMMAND]
        ledgers = sorted({str(line["ledger_name"]) for row in chunk for line in row.lines})
        command = queue_node_command(
            db,
            target=node,
            command_type=TALLY_RECEIPTS_COMMAND,
            payload={
                "franchise_code": node.code,
                "ledgers": [{"name": name} for name in ledgers],
                "receipts": [_command_receipt(row) for row in chunk],
            },
        )
        for row in chunk:
            row.last_command_id = command.public_id
        summary.command_ids.append(command.public_id)
    return summary


def receipt_franchises(db: Session) -> list[FranchiseNode]:
    return list(
        db.scalars(
            select(FranchiseNode)
            .where(
                FranchiseNode.active.is_(True),
                FranchiseNode.tally_receipt_voucher_type.is_not(None),
                FranchiseNode.tally_receipt_voucher_type != "",
            )
            .order_by(FranchiseNode.code)
        )
    )


def local_today() -> date:
    """Today's date on this PC (Tally voucher dates are local, not UTC)."""
    return datetime.now(UTC).astimezone().date()


def _record_sync_result(result: str) -> None:
    with SessionLocal() as db:
        update_settings(db, {SYNC_LAST_AT_KEY: utc_now().isoformat(), SYNC_LAST_RESULT_KEY: result})


def sync_receipts(
    franchise_ids: list[int] | None, from_date: date, to_date: date
) -> list[ReceiptSyncSummary]:
    """Read Tally outside any database transaction, then store each franchise's receipts.

    One franchise's Tally error does not stop the others; errors are kept in the
    last-sync result so the Tally sync page shows why receipts are missing.
    """
    with SessionLocal() as db:
        try:
            config, company = active_tally_source(db)
        except TallyDataError as exc:
            _record_sync_result(f"Failed: {exc}")
            raise
        targets = [
            (node.id, node.code, node.tally_receipt_voucher_type or "")
            for node in receipt_franchises(db)
            if franchise_ids is None or node.id in franchise_ids
        ]
    if not targets:
        raise TallyDataError(
            "No active franchise has a Tally receipt voucher type. Set one on the Franchises page."
        )
    fetched: list[tuple[int, list[ParsedReceipt]]] = []
    errors: list[str] = []
    for node_id, code, voucher_type in targets:
        try:
            fetched.append(
                (node_id, fetch_franchise_receipts(config, company, voucher_type, from_date, to_date))
            )
        except TallyDataError as exc:
            errors.append(f"{code}: {exc}")
    failed = " ".join(f"Failed {error}" for error in errors)
    if not fetched:
        _record_sync_result(failed)
        raise TallyDataError(failed)
    summaries: list[ReceiptSyncSummary] = []
    with SessionLocal() as db:
        for node_id, parsed in fetched:
            node = db.get(FranchiseNode, node_id)
            if node is None or not node.active:
                continue
            summaries.append(apply_franchise_receipts(db, node, parsed))
        result = " ".join([*(summary.describe() for summary in summaries), failed]).strip()
        update_settings(
            db,
            {SYNC_LAST_AT_KEY: utc_now().isoformat(), SYNC_LAST_RESULT_KEY: result},
            commit=False,
        )
        db.commit()
    return summaries


def _bounded_int(db: Session, key: str, default: int, maximum: int) -> int:
    try:
        value = int(get_setting(db, key, str(default)))
    except ValueError:
        return default
    return max(0, min(value, maximum))


def sync_interval_minutes(db: Session) -> int:
    return _bounded_int(db, SYNC_MINUTES_KEY, DEFAULT_SYNC_MINUTES, MAX_SYNC_MINUTES)


def sync_lookback_days(db: Session) -> int:
    return max(1, _bounded_int(db, SYNC_DAYS_KEY, DEFAULT_SYNC_DAYS, MAX_SYNC_DAYS))


def save_sync_schedule(db: Session, minutes_raw: str, days_raw: str) -> None:
    minutes, days = minutes_raw.strip(), days_raw.strip()
    if not minutes.isdigit() or int(minutes) > MAX_SYNC_MINUTES:
        raise ValueError(f"Auto sync interval must be 0 to {MAX_SYNC_MINUTES} minutes.")
    if not days.isdigit() or not 1 <= int(days) <= MAX_SYNC_DAYS:
        raise ValueError(f"Days to look back must be 1 to {MAX_SYNC_DAYS}.")
    update_settings(db, {SYNC_MINUTES_KEY: str(int(minutes)), SYNC_DAYS_KEY: str(int(days))})


def last_sync_at(db: Session) -> datetime | None:
    raw = get_setting(db, SYNC_LAST_AT_KEY, "")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def sync_is_due(db: Session, now: datetime | None = None) -> bool:
    minutes = sync_interval_minutes(db)
    if minutes <= 0 or not receipt_franchises(db):
        return False
    last = last_sync_at(db)
    return last is None or (now or utc_now()) - last >= timedelta(minutes=minutes)


def run_scheduled_receipt_sync() -> list[ReceiptSyncSummary] | None:
    with SessionLocal() as db:
        if not sync_is_due(db):
            return None
        days = sync_lookback_days(db)
    today = local_today()
    return sync_receipts(None, today - timedelta(days=days), today)
