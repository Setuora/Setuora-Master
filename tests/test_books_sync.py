"""Vouchers Tally accepted are mirrored into HNC Books once, as balanced ledger vouchers."""

import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from defusedxml.ElementTree import fromstring

from app.models import Batch, BatchStatus, BatchType
from app.services import books_sync
from app.services import tally as tally_service
from app.services.books_sync import (
    NOTHING_TO_POST,
    BooksClient,
    BooksConfig,
    BooksError,
    BooksUnavailable,
    build_books_xml,
    mirror_to_books,
)
from app.services.tally import TallyResult, sync_batch
from tests.factories import create_batch
from tests.test_tally_reliability import _sale, _Use


class _FakeBooks:
    sent: ClassVar[list[str]] = []
    refuse: ClassVar[set[str]] = set()  # voucher numbers Books refuses
    unavailable: ClassVar[str | None] = None

    def __init__(self, config):
        if _FakeBooks.unavailable:
            raise BooksUnavailable(_FakeBooks.unavailable)

    def import_voucher(self, xml):
        number = fromstring(xml).find(".//VOUCHERNUMBER").text
        if number in _FakeBooks.refuse:
            raise BooksError(f"Books did not take the voucher: Voucher {number}: books are locked")
        _FakeBooks.sent.append(number)
        return "posted"


@pytest.fixture()
def books(db_session, monkeypatch):
    _FakeBooks.sent, _FakeBooks.refuse, _FakeBooks.unavailable = [], set(), None
    monkeypatch.setattr(books_sync, "books_config", lambda: BooksConfig("http://books", "u", "p"))
    monkeypatch.setattr(books_sync, "BooksClient", _FakeBooks)
    monkeypatch.setattr(books_sync, "SessionLocal", lambda: _Use(db_session))
    monkeypatch.setattr(
        tally_service,
        "post_to_tally",
        lambda xml, _settings: TallyResult(xml, "<RESPONSE/>", "CREATED=1; ALTERED=0"),
    )
    return _FakeBooks


def _tally_accepted_sale(db):
    batch = _sale(db, "books")
    sync_batch(db, batch)
    assert batch.status == BatchStatus.SYNCED.value
    return batch


def _another_synced(db, first, n):
    """A copy of a Tally-accepted sale under its own batch number."""
    batch = create_batch(db, first.user, BatchType.SALE, "Customer", "")
    batch.status = BatchStatus.SYNCED.value
    batch.synced_at = first.synced_at
    batch.sync_remote_id = f"remote-{n}"
    batch.sync_request_xml = first.sync_request_xml.replace(
        f"<VOUCHERNUMBER>{first.batch_number}<", f"<VOUCHERNUMBER>{batch.batch_number}<"
    )
    db.commit()
    return batch


def test_tally_accepted_sale_is_mirrored_once_as_balanced_ledger_voucher(
    db_session, books, monkeypatch
):
    batch = _tally_accepted_sale(db_session)
    # Opening stock received into Lite never reaches Tally, so Books skips it too.
    stock_in = create_batch(db_session, batch.user, BatchType.RECEIVE, "Godown", "")
    stock_in.status = BatchStatus.SYNCED.value
    db_session.commit()
    built = []
    monkeypatch.setattr(
        books_sync,
        "build_books_xml",
        lambda b, r: built.append(build_books_xml(b, r)) or built[-1],
    )

    assert mirror_to_books() == 1
    assert mirror_to_books() == 0
    assert books.sent == [batch.batch_number]
    assert batch.books_synced_at is not None and batch.books_error is None
    assert stock_in.books_synced_at is None

    root = fromstring(built[0])
    parents = {ledger.get("NAME"): ledger.findtext("PARENT") for ledger in root.iter("LEDGER")}
    assert parents["Customer"] == "Sundry Debtors"
    assert parents["Sales"] == "Sales Accounts"
    assert parents["CGST"] == parents["SGST"] == "Duties & Taxes"
    voucher = next(root.iter("VOUCHER"))
    assert voucher.get("VCHTYPE") == "Sales"
    assert voucher.findtext("GUID") == f"SETUORA-{batch.sync_remote_id}"
    assert "leave out of Books' Final export" in voucher.findtext("NARRATION")
    amounts = [Decimal(e.findtext("AMOUNT")) for e in voucher.iter("ALLLEDGERENTRIES.LIST")]
    assert sum(amounts) == 0 and len(amounts) >= 3


def test_refused_vouchers_never_hold_up_new_ones_and_wait_for_the_manual_push(db_session, books):
    first = _tally_accepted_sale(db_session)
    second = _another_synced(db_session, first, 2)
    newest = _another_synced(db_session, first, 3)
    books.refuse = {first.batch_number, second.batch_number}

    assert mirror_to_books(limit=2) == 0
    assert "books are locked" in first.books_error and "books are locked" in second.books_error
    # The timed worker skips the two refusals, however small the limit.
    assert mirror_to_books(limit=2) == 1
    assert newest.books_synced_at is not None
    assert mirror_to_books(limit=2) == 0
    assert books.sent == [newest.batch_number]

    books.refuse = set()
    assert mirror_to_books(limit=2, retry_rejected=True) == 2
    assert first.books_error is None and second.books_synced_at is not None


def test_books_unavailable_marks_no_voucher(db_session, books):
    batch = _tally_accepted_sale(db_session)
    books.unavailable = "Books is not reachable: connection refused"
    with pytest.raises(BooksUnavailable, match="not reachable"):
        mirror_to_books()
    assert batch.books_error is None and batch.books_synced_at is None

    books.unavailable = None
    assert mirror_to_books() == 1


def test_mirror_from_date_skips_older_vouchers(db_session, books, monkeypatch):
    batch = _tally_accepted_sale(db_session)
    later = batch.synced_at.date().replace(year=batch.synced_at.year + 1)
    monkeypatch.setattr(
        books_sync, "books_config", lambda: BooksConfig("http://books", "u", "p", later)
    )
    assert mirror_to_books() == 0 and batch.books_synced_at is None


def _tally_request(entries: str, inventory: str = "") -> str:
    return (
        "<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA><TALLYMESSAGE>"
        '<VOUCHER VCHTYPE="X"><DATE>20261127</DATE><VOUCHERTYPENAME>CREDIT NOTE CHN</VOUCHERTYPENAME>'
        "<VOUCHERNUMBER>N-1</VOUCHERNUMBER><NARRATION>Setuora Master network batch N-1</NARRATION>"
        f"{entries}{inventory}</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


def _entry(name, amount, party=False):
    flag = "<ISPARTYLEDGER>Yes</ISPARTYLEDGER>" if party else ""
    return f"<LEDGERENTRIES.LIST><LEDGERNAME>{name}</LEDGERNAME>{flag}<AMOUNT>{amount}</AMOUNT></LEDGERENTRIES.LIST>"


def _item(ledger, amount):
    return (
        "<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>Item</STOCKITEMNAME><ACCOUNTINGALLOCATIONS.LIST>"
        f"<LEDGERNAME>{ledger}</LEDGERNAME><AMOUNT>{amount}</AMOUNT>"
        "</ACCOUNTINGALLOCATIONS.LIST></ALLINVENTORYENTRIES.LIST>"
    )


def _built(batch_type, xml, round_off="ROUND OFF"):
    batch = Batch(
        batch_number="N-1", batch_type=batch_type, sync_request_xml=xml, sync_remote_id="r1"
    )
    out = build_books_xml(batch, round_off)
    if out is None:
        return None
    root = fromstring(out)
    parents = {ledger.get("NAME"): ledger.findtext("PARENT") for ledger in root.iter("LEDGER")}
    voucher = next(root.iter("VOUCHER"))
    lines = [
        (e.findtext("LEDGERNAME"), Decimal(e.findtext("AMOUNT")))
        for e in voucher.iter("ALLLEDGERENTRIES.LIST")
    ]
    return voucher.get("VCHTYPE"), parents, lines


def test_credit_note_with_round_off_zero_line_and_long_ledger_name():
    long_name = "L" * 150
    xml = _tally_request(
        _entry(long_name, "56.00", party=True)
        + _entry("CGST", "-1.33")
        + _entry("SGST", "-1.33")
        + _entry("Round Off", "-0.01"),
        _item("Sales", "-53.33") + _item("Sales", "0.00"),
    )
    vchtype, parents, lines = _built(BatchType.SALES_RETURN.value, xml)
    assert vchtype == "Credit Note"
    assert parents == {
        "L" * 120: "Sundry Debtors",
        "CGST": "Duties & Taxes",
        "SGST": "Duties & Taxes",
        "Round Off": "Indirect Expenses",
        "Sales": "Sales Accounts",
    }
    assert all(amount != 0 for _name, amount in lines) and sum(a for _n, a in lines) == 0
    assert lines[0][0] == "L" * 120


def test_purchase_uses_creditor_and_purchase_groups_and_all_zero_has_nothing_to_post():
    xml = _tally_request(
        _entry("Supplier", "105.00", party=True) + _entry("Input CGST", "-2.50"),
        _item("Purchase", "-102.50"),
    )
    vchtype, parents, _lines = _built(BatchType.PURCHASE.value, xml)
    assert vchtype == "Purchase"
    assert parents["Supplier"] == "Sundry Creditors" and parents["Purchase"] == "Purchase Accounts"

    free = _tally_request(_entry("Customer", "0.00", party=True), _item("Sales", "0.00"))
    assert _built(BatchType.SALE.value, free) is None


def test_zero_value_voucher_is_marked_without_posting(db_session, books):
    batch = _tally_accepted_sale(db_session)
    batch.sync_request_xml = _tally_request(
        _entry("Customer", "0.00", party=True), _item("Sales", "0")
    )
    db_session.commit()
    assert mirror_to_books() == 0
    assert batch.books_synced_at is not None and batch.books_error == NOTHING_TO_POST
    assert books.sent == []


def test_books_reply_pages_are_read(monkeypatch):
    client = BooksClient.__new__(BooksClient)
    client.base, client.csrf = "http://books", "token"

    def reply(page, path="/tally/import"):
        monkeypatch.setattr(client, "_open", lambda request: (path, page))

    reply("<p>Groups 0, ledgers 3, vouchers 1, skipped 0</p><ul class=bad></ul>")
    assert client.import_voucher("<ENVELOPE/>") == "posted"
    reply("<p>Groups 0, ledgers 0, vouchers 0, skipped 1</p><ul class=bad></ul>")
    assert client.import_voucher("<ENVELOPE/>") == "already in Books"
    reply(
        "<p>Groups 0, ledgers 0, vouchers 0, skipped 0</p>"
        "<ul class=bad><li>Voucher A1: Unknown ledger: X &amp; Y</li></ul>"
    )
    with pytest.raises(BooksError, match="Unknown ledger: X & Y"):
        client.import_voucher("<ENVELOPE/>")
    reply("<nav><li>Menu</li></nav><p class=bad>The file is not valid XML</p>")
    with pytest.raises(BooksError, match="not valid XML"):
        client.import_voucher("<ENVELOPE/>")
    reply("<form>login</form>", path="/login")
    with pytest.raises(BooksUnavailable, match="session was refused"):
        client.import_voucher("<ENVELOPE/>")


class _StubBooks(BaseHTTPRequestHandler):
    """Just enough of Books: cookie login with a 303, and a CSRF-checked import."""

    must_change = False

    def log_message(self, *args):
        pass

    def _page(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/login":
            ok = b"password=right" in body
            self.send_response(303)
            if ok:
                self.send_header("Set-Cookie", "books_session=s1; HttpOnly; Path=/")
                self.send_header("Set-Cookie", "books_csrf=c1; Path=/")
            self.send_header("Location", "/" if ok else "/login?msg=Wrong")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        signed = self.headers.get("X-CSRF-Token") == "c1"
        logged_in = "books_session=s1" in (self.headers.get("Cookie") or "")
        if signed and logged_in and b"<VOUCHER" in body:
            self._page(200, "<p>Groups 0, ledgers 2, vouchers 1, skipped 0</p><ul class=bad></ul>")
        else:
            self._page(403, "Security check failed")

    def do_GET(self):
        if _StubBooks.must_change and self.path == "/":
            self.send_response(303)
            self.send_header("Location", "/password")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._page(200, "<h1>Books</h1>")


@pytest.fixture()
def stub_books():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubBooks)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _StubBooks.must_change = False
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_client_logs_in_with_cookies_and_signs_the_import(stub_books):
    client = BooksClient(BooksConfig(stub_books, "setuora", "right"))
    assert client.import_voucher("<ENVELOPE><VOUCHER/></ENVELOPE>") == "posted"

    with pytest.raises(BooksUnavailable, match="login failed"):
        BooksClient(BooksConfig(stub_books, "setuora", "wrong"))
    _StubBooks.must_change = True
    with pytest.raises(BooksUnavailable, match="password changed"):
        BooksClient(BooksConfig(stub_books, "setuora", "right"))


def test_manual_push_rotates_through_every_refused_voucher(db_session, books):
    first = _tally_accepted_sale(db_session)
    second = _another_synced(db_session, first, 2)
    third = _another_synced(db_session, first, 3)
    books.refuse = {first.batch_number, second.batch_number, third.batch_number}
    assert mirror_to_books(limit=2) == 0
    assert mirror_to_books(limit=2) == 0
    assert third.books_error is not None

    # Only the third can be fixed; the two older refusals must not hide it.
    books.refuse = {first.batch_number, second.batch_number}
    assert mirror_to_books(limit=2, retry_rejected=True) == 0
    assert mirror_to_books(limit=2, retry_rejected=True) == 1
    assert third.books_synced_at is not None and books.sent == [third.batch_number]


def test_party_found_by_party_ledger_name_in_older_requests():
    xml = _tally_request(
        "<PARTYLEDGERNAME>Old Customer</PARTYLEDGERNAME>"
        "<LEDGERENTRIES.LIST><LEDGERNAME>Old Customer</LEDGERNAME><AMOUNT>-105.00</AMOUNT>"
        "</LEDGERENTRIES.LIST>" + _entry("CGST", "5.00"),
        _item("Sales", "100.00"),
    )
    _vchtype, parents, _lines = _built(BatchType.SALE.value, xml)
    assert parents["Old Customer"] == "Sundry Debtors" and parents["CGST"] == "Duties & Taxes"


def test_http_error_from_books_means_unavailable(stub_books):
    client = BooksClient(BooksConfig(stub_books, "setuora", "right"))
    client.csrf = "stale"
    with pytest.raises(BooksUnavailable, match="HTTP 403"):
        client.import_voucher("<ENVELOPE><VOUCHER/></ENVELOPE>")


def test_manual_push_reports_books_outcome(monkeypatch):
    from urllib.parse import unquote_plus

    from app.main import app
    from app.models import Setting
    from app.routers import tally_sync
    from tests.factories import authenticate_client
    from tests.test_account import _client_with_user

    client, Session, engine = _client_with_user(must_change=False)
    with Session() as db:
        db.add(Setting(key="tally_enabled", value="true"))
        db.commit()
    monkeypatch.setattr(tally_sync, "retry_pending_batches", lambda limit: 0)
    calls = []

    def down(**kwargs):
        calls.append(kwargs)
        raise BooksUnavailable("Books is not reachable: refused")

    try:
        authenticate_client(client, 1)
        monkeypatch.setattr(tally_sync, "mirror_to_books", down)
        refused = unquote_plus(client.post("/tally-sync/push").headers["location"])
        monkeypatch.setattr(tally_sync, "mirror_to_books", lambda **kwargs: 2)
        copied = unquote_plus(client.post("/tally-sync/push").headers["location"])
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
    assert calls == [{"retry_rejected": True}]
    assert "HNC Books: Books is not reachable: refused" in refused
    assert "Copied 2 to HNC Books." in copied
