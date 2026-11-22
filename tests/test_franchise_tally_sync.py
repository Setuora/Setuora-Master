import json
from datetime import date
from uuid import uuid4
from xml.etree import ElementTree as ET  # nosec B405 - parses test XML only

import pytest
from sqlalchemy import select

from app.models import Batch, BatchType, NodeCommand, TallyReceipt
from app.services.network_ingest import TALLY_RECEIPTS_COMMAND
from app.services.tally import build_voucher_xml
from app.services.tally_masters import TallyDataError
from app.services.tally_receipts import (
    apply_franchise_receipts,
    build_receipt_vouchers_xml,
    parse_receipt_vouchers,
)
from tests.test_network_sync import (  # noqa: F401 - api is a pytest fixture
    api,
    auth,
    create_node_with_key,
    event,
    item,
    post,
    seed_master_qr,
)

RECEIPT_TYPE = "Recipets Chintamani Warehouse"
SETTINGS = {
    "company_name": "HOME NEEDS CART PRIVATE LIMITED",
    "round_off_ledger_name": "ROUND OFF",
    "sales_gst_ledger_mappings": "18 | Sales 18% | CGST 9% | SGST 9% | IGST 18%",
}


def receipt_response(*vouchers: str) -> ET.Element:
    return ET.fromstring(
        f"<ENVELOPE><BODY><DATA><COLLECTION>{''.join(vouchers)}</COLLECTION></DATA></BODY></ENVELOPE>"
    )


def voucher(guid: str, number: str, day: str, alter_id: str, *lines: tuple[str, str]) -> str:
    entries = "".join(
        f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{name}</LEDGERNAME><AMOUNT>{amount}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        for name, amount in lines
    )
    return (
        f'<VOUCHER VCHTYPE="{RECEIPT_TYPE}"><DATE>{day}</DATE><GUID>{guid}</GUID>'
        f"<VOUCHERNUMBER>{number}</VOUCHERNUMBER><VOUCHERTYPENAME>{RECEIPT_TYPE}</VOUCHERTYPENAME>"
        f"<MASTERID>1</MASTERID><ALTERID>{alter_id}</ALTERID><NARRATION>UPI</NARRATION>"
        f"{entries}</VOUCHER>"
    )


SPLIT_RECEIPT = voucher(
    "guid-1",
    "16",
    "20260522",
    "100",
    ("GOWTHAMI SR 9901477018", "2996.46"),
    ("NAVEEN CHANDRA 9008430928", "2996.46"),
    ("KOTAK MAHINDRA BANK", "-5992.92"),
)
SINGLE_RECEIPT = voucher(
    "guid-2",
    "10",
    "20260720",
    "200",
    ("V SIVA KUMARI 9949481549", "2993.00"),
    ("RBL BANK LIMITED3338", "-2993.00"),
)


def test_receipt_parser_reads_customers_bank_and_skips_empty_vouchers():
    root = receipt_response("<VOUCHER><DATE></DATE></VOUCHER>", SINGLE_RECEIPT, SPLIT_RECEIPT)

    receipts = parse_receipt_vouchers(root, RECEIPT_TYPE)

    assert [receipt.voucher_number for receipt in receipts] == ["16", "10"]
    split = receipts[0]
    assert split.voucher_date == date(2026, 5, 22)
    assert split.bank_ledger == "KOTAK MAHINDRA BANK"
    assert [line.ledger_name for line in split.lines] == [
        "GOWTHAMI SR 9901477018",
        "NAVEEN CHANDRA 9008430928",
    ]
    assert str(split.total_amount) == "5992.92"


def test_receipt_request_filters_by_voucher_type_and_dates():
    xml = build_receipt_vouchers_xml(
        "HOME NEEDS CART", RECEIPT_TYPE, date(2026, 10, 1), date(2026, 10, 31)
    )

    assert f'$VoucherTypeName = "{RECEIPT_TYPE}"' in xml
    assert '<SVFROMDATE TYPE="Date">20261001</SVFROMDATE>' in xml
    assert '<SVTODATE TYPE="Date">20261031</SVTODATE>' in xml
    with pytest.raises(TallyDataError):
        build_receipt_vouchers_xml(
            "HOME NEEDS CART", 'Bad" OR 1', date(2026, 1, 1), date(2026, 1, 2)
        )


def test_receipts_are_stored_once_and_only_changes_are_sent_to_lite(api):  # noqa: F811
    client, db = api
    node, api_key = create_node_with_key(db, "CHINTAMANI")
    node.tally_receipt_voucher_type = RECEIPT_TYPE
    db.commit()
    parsed = parse_receipt_vouchers(receipt_response(SPLIT_RECEIPT, SINGLE_RECEIPT), RECEIPT_TYPE)

    first = apply_franchise_receipts(db, node, parsed)
    db.commit()
    assert (first.created, first.updated, first.unchanged) == (2, 0, 0)
    commands = db.scalars(select(NodeCommand)).all()
    assert [command.command_type for command in commands] == [TALLY_RECEIPTS_COMMAND]
    payload = json.loads(commands[0].payload_json)
    assert {ledger["name"] for ledger in payload["ledgers"]} == {
        "GOWTHAMI SR 9901477018",
        "NAVEEN CHANDRA 9008430928",
        "V SIVA KUMARI 9949481549",
    }
    assert payload["receipts"][0]["bank_ledger"] == "KOTAK MAHINDRA BANK"

    again = apply_franchise_receipts(db, node, parsed)
    db.commit()
    assert (again.created, again.updated, again.unchanged) == (0, 0, 2)
    assert len(db.scalars(select(NodeCommand)).all()) == 1

    edited = voucher(
        "guid-2",
        "10",
        "20260720",
        "201",
        ("V SIVA KUMARI 9949481549", "3000.00"),
        ("RBL BANK LIMITED3338", "-3000.00"),
    )
    changed = apply_franchise_receipts(
        db, node, parse_receipt_vouchers(receipt_response(edited), RECEIPT_TYPE)
    )
    db.commit()
    assert (changed.created, changed.updated) == (0, 1)
    assert len(db.scalars(select(TallyReceipt)).all()) == 2
    assert len(db.scalars(select(NodeCommand)).all()) == 2

    # Lite only receives Tally receipts once it says it understands them.
    withheld = client.get("/api/v1/commands", headers=auth(api_key))
    assert withheld.json()["data"]["commands"] == []
    offered = client.get(
        "/api/v1/commands",
        headers={**auth(api_key), "X-Setuora-Command-Types": TALLY_RECEIPTS_COMMAND},
    )
    assert [command["type"] for command in offered.json()["data"]["commands"]] == [
        TALLY_RECEIPTS_COMMAND,
        TALLY_RECEIPTS_COMMAND,
    ]


def test_franchise_sale_uses_its_own_tally_sales_voucher_type(api):  # noqa: F811
    client, db = api
    node, api_key = create_node_with_key(db, "CHINTAMANI")
    node.tally_godown_name = "Chintamani Warehouse"
    node.tally_sales_voucher_type = "Chintamani Warehouse Sale"
    db.commit()
    stock = item("QR-CHINTAMANI-SALE")
    seed_master_qr(db, node, stock)

    response = post(
        client,
        api_key,
        event(1, "PURCHASE", [stock], party_name="Supplier"),
        event(
            2,
            "SALE",
            [item("QR-CHINTAMANI-SALE", status="SOLD")],
            party_name="V SIVA KUMARI 9949481549",
        ),
    )
    assert response.status_code == 200, response.text

    sale = db.scalar(select(Batch).where(Batch.batch_type == BatchType.SALE.value))
    purchase = db.scalar(select(Batch).where(Batch.batch_type == BatchType.PURCHASE.value))
    assert sale.tally_voucher_type == "Chintamani Warehouse Sale"
    assert purchase.tally_voucher_type is None
    xml = build_voucher_xml(sale, SETTINGS)
    assert 'VCHTYPE="Chintamani Warehouse Sale"' in xml
    assert "<VOUCHERTYPENAME>Chintamani Warehouse Sale</VOUCHERTYPENAME>" in xml
    assert "<PARTYLEDGERNAME>V SIVA KUMARI 9949481549</PARTYLEDGERNAME>" in xml
    assert "<GODOWNNAME>Chintamani Warehouse</GODOWNNAME>" in xml


def _transfer(client, db, *, source_godown, destination_godown):
    source, source_key = create_node_with_key(db, "MAIN")
    destination, destination_key = create_node_with_key(db, "CHINTAMANI")
    source.tally_godown_name = source_godown
    destination.tally_godown_name = destination_godown
    db.commit()
    first, second = item("QR-T-1"), item("QR-T-2")
    seed_master_qr(db, source, first)
    seed_master_qr(db, source, second)
    assert (
        post(
            client, source_key, event(1, "PURCHASE", [first, second], party_name="Supplier")
        ).status_code
        == 200
    )
    transfer_id = str(uuid4())
    dispatched = post(
        client,
        source_key,
        event(
            2,
            "TRANSFER_DISPATCHED",
            [first, second],
            event_id=transfer_id,
            reference="ST-20",
            destination_franchise_code=destination.code,
        ),
    )
    assert dispatched.status_code == 200, dispatched.text
    received = post(
        client,
        destination_key,
        event(1, "TRANSFER_RECEIVED", [first, second], transfer_id=transfer_id),
    )
    assert received.status_code == 200, received.text


def test_received_transfer_posts_a_godown_to_godown_stock_journal(api):  # noqa: F811
    client, db = api
    _transfer(client, db, source_godown="Main Location", destination_godown="Chintamani Warehouse")

    journal = db.scalar(select(Batch).where(Batch.batch_type == BatchType.STOCK_TRANSFER.value))
    assert journal is not None
    assert journal.status == "PENDING_SYNC"
    assert journal.tally_source_location == "Main Location"
    assert journal.tally_stock_location == "Chintamani Warehouse"

    root = ET.fromstring(build_voucher_xml(journal, SETTINGS))
    voucher_node = root.find(".//VOUCHER")
    assert voucher_node.get("VCHTYPE") == "Stock Journal"
    outward = voucher_node.findall("INVENTORYENTRIESOUT.LIST")
    inward = voucher_node.findall("INVENTORYENTRIESIN.LIST")
    assert [entry.findtext("BATCHALLOCATIONS.LIST/GODOWNNAME") for entry in outward] == [
        "Main Location"
    ]
    assert [entry.findtext("BATCHALLOCATIONS.LIST/GODOWNNAME") for entry in inward] == [
        "Chintamani Warehouse"
    ]
    assert outward[0].findtext("ACTUALQTY") == "2 Pcs"
    assert float(outward[0].findtext("AMOUNT")) == -float(inward[0].findtext("AMOUNT")) == 200.0


def test_transfer_without_franchise_godowns_does_not_touch_tally(api):  # noqa: F811
    client, db = api
    _transfer(client, db, source_godown=None, destination_godown=None)

    assert (
        db.scalar(select(Batch).where(Batch.batch_type == BatchType.STOCK_TRANSFER.value)) is None
    )


def test_receipt_sync_keeps_going_past_one_franchise_and_records_errors(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base
    from app.models import Setting
    from app.services import tally_receipts
    from app.services.node_auth import create_franchise_node

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(tally_receipts, "SessionLocal", session_factory)
    monkeypatch.setattr(tally_receipts, "active_tally_source", lambda _db: ({}, "COMPANY"))
    with session_factory() as db:
        for code in ("GOOD", "BAD"):
            node = create_franchise_node(db, code=code, name=code, location=code)
            node.tally_receipt_voucher_type = f"Receipt {code}"
        db.commit()

    def fetch(_config, _company, voucher_type, _from, _to):
        if voucher_type == "Receipt BAD":
            raise TallyDataError("Tally gateway timed out.")
        return parse_receipt_vouchers(receipt_response(SINGLE_RECEIPT), voucher_type)

    monkeypatch.setattr(tally_receipts, "fetch_franchise_receipts", fetch)

    summaries = tally_receipts.sync_receipts(None, date(2026, 7, 1), date(2026, 7, 31))

    assert [(summary.franchise_code, summary.created) for summary in summaries] == [("GOOD", 1)]
    with session_factory() as db:
        result = db.get(Setting, tally_receipts.SYNC_LAST_RESULT_KEY).value
        assert db.scalars(select(TallyReceipt)).one().voucher_number == "10"
    assert "GOOD: read 1 receipts" in result
    assert "Failed BAD: Tally gateway timed out." in result
    engine.dispose()
