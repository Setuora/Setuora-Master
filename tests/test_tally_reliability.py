"""Tally being closed, logged out or slow must not lose or duplicate vouchers."""

from datetime import timedelta
from urllib.error import URLError
from xml.etree import ElementTree as ET

import pytest
from defusedxml.ElementTree import fromstring

from app.models import BatchStatus, BatchType, Product, SerialStatus, Setting, SyncAttempt, User
from app.services import sync_worker
from app.services import tally as tally_service
from app.services.tally import TallySyncError, post_to_tally, sync_batch
from app.services.tally_masters import TallyDataError
from tests.factories import (
    add_serial_to_batch,
    apply_batch_statuses,
    create_batch,
    generate_serials,
)

SETTINGS = {
    "company_name": "Setuora Test Company",
    "tally_enabled": "true",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "sales_voucher_type": "Sales",
    "sales_ledger_name": "Sales",
    "sales_gst_ledger_mappings": "5 | Sales | CGST | SGST | IGST",
    "round_off_ledger_name": "Round Off",
}


class _Use:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


def _sale(db, name="reliable"):
    user = User(username=name, password_hash="x", role="sales")
    product = Product(
        product_code=f"P-{name}",
        product_name="Masala",
        hsn="0910",
        gst_rate=5,
        unit="Pcs",
        default_rate=100,
        tally_stock_item_name="Masala",
    )
    db.add_all([user, product])
    db.add_all(Setting(key=key, value=value) for key, value in SETTINGS.items())
    db.commit()
    serial = generate_serials(db, product, 1, initial_status=SerialStatus.IN_STOCK)[0]
    batch = create_batch(db, user, BatchType.SALE, "Customer", "")
    add_serial_to_batch(db, batch, user, serial.serial_number)
    apply_batch_statuses(db, batch, user)
    db.commit()
    return batch


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_closed_company_and_refused_connection_mean_not_imported(monkeypatch):
    monkeypatch.setattr(tally_service, "urlopen", lambda request, timeout: _Response())
    monkeypatch.setattr(
        tally_service,
        "read_tally_response",
        lambda response: (
            "<RESPONSE><LINEERROR>Could not set 'SVCurrentCompany' to 'X'</LINEERROR></RESPONSE>"
        ),
    )
    with pytest.raises(TallySyncError) as closed:
        post_to_tally("<ENVELOPE/>", SETTINGS)
    assert (closed.value.not_imported, closed.value.retryable, closed.value.outcome_unknown) == (
        True,
        True,
        False,
    )

    def refused(request, timeout):
        raise URLError(ConnectionRefusedError())

    monkeypatch.setattr(tally_service, "urlopen", refused)
    with pytest.raises(TallySyncError) as down:
        post_to_tally("<ENVELOPE/>", SETTINGS)
    assert (down.value.not_imported, down.value.outcome_unknown) == (True, False)


def test_sale_waits_for_tally_however_long_it_is_closed(db_session, monkeypatch):
    batch = _sale(db_session)
    attempts = []

    def closed(xml, _settings):
        attempts.append(xml)
        raise TallySyncError("company not open", retryable=True, request_xml=xml, not_imported=True)

    monkeypatch.setattr(tally_service, "post_to_tally", closed)
    for _ in range(8):
        sync_batch(db_session, batch)

    # Past the three-retry cap, yet still queued: Tally never saw the voucher.
    assert len(attempts) == 8
    assert batch.status == BatchStatus.PENDING_SYNC.value


def _posted_reply(number: str, narration: str):
    return "", fromstring(
        "<ENVELOPE><BODY><DATA><COLLECTION>"
        f"<VOUCHER><VOUCHERNUMBER>{number}</VOUCHERNUMBER><NARRATION>{narration}</NARRATION></VOUCHER>"
        "</COLLECTION></DATA></BODY></ENVELOPE>"
    )


def test_find_posted_voucher_matches_exact_narration_on_the_voucher_date(monkeypatch):
    request_xml = (
        "<ENVELOPE><BODY><VOUCHER><DATE>20261122</DATE>"
        '<NARRATION>Setuora Master network batch NET-1 "x"</NARRATION></VOUCHER></BODY></ENVELOPE>'
    )
    sent = []

    def lookup(settings, xml, *, timeout):
        sent.append(xml)
        return _posted_reply("HNCC/2/26-27", 'Setuora Master network batch NET-1 "x"')

    monkeypatch.setattr(tally_service, "_post_read_request", lookup)
    assert tally_service.find_posted_voucher(SETTINGS, request_xml) == "HNCC/2/26-27"
    query = ET.fromstring(sent[0])
    assert (
        query.find(".//STATICVARIABLES/SETUORANARRATION").text
        == 'Setuora Master network batch NET-1 "x"'
    )
    assert query.find(".//STATICVARIABLES/SVFROMDATE").text == "20261122"
    assert "##SetuoraNarration" in query.find(".//SYSTEM").text

    monkeypatch.setattr(
        tally_service,
        "_post_read_request",
        lambda s, x, *, timeout: _posted_reply("9", "other batch"),
    )
    assert tally_service.find_posted_voucher(SETTINGS, request_xml) is None


def _review_batch(db):
    batch = _sale(db, name="review")
    batch.status = BatchStatus.REVIEW_REQUIRED.value
    batch.sync_request_xml = (
        "<ENVELOPE><BODY><VOUCHER><DATE>20261122</DATE>"
        "<NARRATION>Setuora Master network batch X</NARRATION></VOUCHER></BODY></ENVELOPE>"
    )
    old = SyncAttempt(batch_id=batch.id, status=batch.status, request_xml=batch.sync_request_xml)
    db.add(old)
    db.commit()
    old.created_at = old.created_at - timedelta(minutes=30)
    db.commit()
    return batch, old


@pytest.mark.parametrize(
    ("found", "status", "reference"),
    [
        ("HNCC/7/26-27", BatchStatus.SYNCED.value, "Found in Tally as HNCC/7/26-27"),
        (None, BatchStatus.PENDING_SYNC.value, None),
    ],
)
def test_review_batch_settles_itself_from_tally(db_session, monkeypatch, found, status, reference):
    batch, _ = _review_batch(db_session)
    original_reference = batch.tally_reference
    monkeypatch.setattr(sync_worker, "SessionLocal", lambda: _Use(db_session))
    monkeypatch.setattr(sync_worker, "find_posted_voucher", lambda settings, xml: found)

    assert sync_worker.reconcile_review_batches() == 1
    db_session.refresh(batch)
    assert batch.status == status
    assert batch.tally_reference == (reference or original_reference)


def test_review_batch_waits_while_recent_or_while_tally_cannot_answer(db_session, monkeypatch):
    batch, attempt = _review_batch(db_session)
    monkeypatch.setattr(sync_worker, "SessionLocal", lambda: _Use(db_session))

    def unavailable(settings, xml):
        raise TallyDataError("Tally gateway timed out.")

    monkeypatch.setattr(sync_worker, "find_posted_voucher", unavailable)
    assert sync_worker.reconcile_review_batches() == 0
    assert batch.status == BatchStatus.REVIEW_REQUIRED.value

    # A fresh attempt may still be importing inside Tally: do not look yet.
    calls = []
    monkeypatch.setattr(sync_worker, "find_posted_voucher", lambda s, x: calls.append(x))
    attempt.created_at = attempt.created_at + timedelta(minutes=29)
    db_session.commit()
    assert sync_worker.reconcile_review_batches() == 0
    assert calls == []
    assert batch.status == BatchStatus.REVIEW_REQUIRED.value
