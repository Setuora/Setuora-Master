"""Setup-phase QR creation from a franchise's Tally godown stock."""

from defusedxml.ElementTree import fromstring
from sqlalchemy import func, select

from app.models import NetworkStock, NodeCommand, Product, Serial
from app.services import tally_godown_qr as godown_qr
from app.services.network_ingest import counts_as_franchise_stock
from app.services.node_auth import create_franchise_node
from app.services.tally_masters import TallyStockItem

GODOWN = "CHINTHAMANI WAREHOUSE"


def test_labels_count_pieces_and_kg_packets():
    assert godown_qr.labels_for("SG PAPAYA FACE WASH 100ML", 560, "PCS") == (560, "")
    assert godown_qr.labels_for("BGL SUGAR 500GM", 15, "KG")[0] == 30
    assert godown_qr.labels_for("BGL TOORDAL 1KG", 25, "KG")[0] == 25
    assert godown_qr.labels_for("BGL SUGAR 1 KG", 20, "KG")[0] == 20
    assert godown_qr.labels_for("BHAGYALAKSHMI MOONG -500 GRM", 2, "KG")[0] == 4
    assert godown_qr.labels_for("LOOSE RICE", 10, "KG")[0] == 0
    assert godown_qr.labels_for("BGL SUGAR 400GM", 1, "KG")[0] == 0  # 2.5 packets
    assert godown_qr.labels_for("ITEM", 2.5, "PCS")[0] == 0
    assert godown_qr.labels_for("ITEM", 0, "PCS")[0] == 0


def test_item_request_quotes_the_name_so_tally_does_not_read_a_formula():
    xml = godown_qr._item_batches_xml("Company & Co", "100 MM GRENDING WHEEL")
    childof = fromstring(xml).find(".//COLLECTION/CHILDOF")
    assert childof.text == '"100 MM GRENDING WHEEL"'


def _reply(body: str):
    return "", fromstring(f"<ENVELOPE><BODY><DATA><COLLECTION>{body}</COLLECTION></DATA></BODY></ENVELOPE>")


def test_fetch_reads_only_the_franchise_godown_and_skips_quoted_names(monkeypatch):
    requests = []
    batches = {
        "SUGAR 500GM": (
            '<BATCH NAME="Primary Batch"><GODOWNNAME>CHINTHAMANI WAREHOUSE</GODOWNNAME>'
            "<CLOSINGBALANCE> 15.00 KG</CLOSINGBALANCE></BATCH>"
            '<BATCH NAME="Primary Batch"><GODOWNNAME>Main Location</GODOWNNAME>'
            "<CLOSINGBALANCE> 99.00 KG</CLOSINGBALANCE></BATCH>"
        ),
        "WHEEL": (
            '<BATCH NAME="Primary Batch"><GODOWNNAME>Main Location</GODOWNNAME>'
            "<CLOSINGBALANCE> 16.00 PCS</CLOSINGBALANCE></BATCH>"
        ),
    }

    def fake_post(settings, xml, *, timeout=8):
        requests.append(xml)
        if "Setuora Item Closing List" in xml:
            return _reply(
                '<STOCKITEM NAME="SUGAR 500GM"><CLOSINGBALANCE> 114.00 KG</CLOSINGBALANCE></STOCKITEM>'
                '<STOCKITEM NAME="WHEEL"><CLOSINGBALANCE> 16.00 PCS</CLOSINGBALANCE></STOCKITEM>'
                '<STOCKITEM NAME="EMPTY"><CLOSINGBALANCE></CLOSINGBALANCE></STOCKITEM>'
                '<STOCKITEM NAME="14&quot; Cut Off Wheel"><CLOSINGBALANCE> 3.00 PCS</CLOSINGBALANCE></STOCKITEM>'
            )
        for name, body in batches.items():
            if f"&quot;{name}&quot;" in xml or f'"{name}"' in xml:
                return _reply(body)
        raise AssertionError("unexpected request")

    monkeypatch.setattr(godown_qr, "_post_read_request", fake_post)
    monkeypatch.setattr(godown_qr, "ITEM_PAUSE_SECONDS", 0)

    quantities, skipped = godown_qr.fetch_godown_quantities({}, "Company", GODOWN)

    assert quantities == {"SUGAR 500GM": (15.0, "KG")}
    assert skipped == ['14" Cut Off Wheel']
    assert len(requests) == 3  # the list, then only the two items with stock


def _items():
    return [
        TallyStockItem(name="FACE WASH 100ML", unit="PCS", hsn="3304", gst_rate=18, standard_rate=120),
        TallyStockItem(name="SUGAR 500GM", unit="KG", hsn="1701", gst_rate=5, standard_rate=25),
        TallyStockItem(name="NO GST ITEM", unit="PCS", hsn="", gst_rate=None),
    ]


def test_plan_creates_serials_once_and_counts_held_stock_later(db_session):
    node = create_franchise_node(db_session, code="CHN-01", name="Chintamani", location="Chintamani")
    node.tally_godown_name = GODOWN
    db_session.commit()
    quantities = {"FACE WASH 100ML": (3.0, "PCS"), "SUGAR 500GM": (1.0, "KG"), "NO GST ITEM": (4.0, "PCS")}

    plan = godown_qr.plan_from_stock(db_session, node, quantities, _items(), [])
    by_item = {line["item"]: line for line in plan["lines"]}
    assert by_item["FACE WASH 100ML"]["to_create"] == 3
    assert by_item["SUGAR 500GM"]["to_create"] == 2
    assert by_item["NO GST ITEM"]["to_create"] == 0
    assert "GST" in by_item["NO GST ITEM"]["note"]

    assert godown_qr.create_from_plan(db_session, node, plan, plan["token"], created_by_id=None) == (2, 5)
    # A repeated submit of the same preview reuses its allocations.
    assert godown_qr.create_from_plan(db_session, node, plan, plan["token"], created_by_id=None) == (2, 5)
    assert db_session.scalar(select(func.count(Serial.id))) == 5
    assert db_session.scalar(select(func.count(NetworkStock.id))) == 5
    codes = set(db_session.scalars(select(Product.product_code)))
    assert {"CHN-01:FACE-WASH-100ML", "CHN-01:SUGAR-500GM"} <= codes
    command_types = set(db_session.scalars(select(NodeCommand.command_type)))
    assert {"PRODUCT_UPSERT", "QR_ALLOCATED"} <= command_types

    again = godown_qr.plan_from_stock(db_session, node, {"FACE WASH 100ML": (5.0, "PCS")}, _items(), [])
    line = again["lines"][0]
    assert (line["labels"], line["already_held"], line["to_create"]) == (5, 3, 2)


def test_issued_tally_stock_counts_at_the_franchise_and_reconciles(db_session):
    node = create_franchise_node(db_session, code="CHN-03", name="Chintamani", location="Chintamani")
    node.tally_godown_name = GODOWN
    db_session.commit()
    quantities = {"FACE WASH 100ML": (3.0, "PCS"), "SUGAR 500GM": (1.0, "KG")}
    plan = godown_qr.plan_from_stock(db_session, node, quantities, _items(), [])
    godown_qr.create_from_plan(db_session, node, plan, plan["token"], created_by_id=None)

    # Not booked in on Lite yet, but Tally already holds it at the franchise.
    held = db_session.scalar(
        select(func.count(NetworkStock.id)).where(
            NetworkStock.current_franchise_id == node.id, counts_as_franchise_stock()
        )
    )
    assert held == 5

    # Tally later shows 1 face wash less and no sugar: both are flagged, nothing is created.
    later = godown_qr.plan_from_stock(db_session, node, {"FACE WASH 100ML": (2.0, "PCS")}, _items(), [])
    by_item = {line["item"]: line for line in later["lines"]}
    assert later["mismatches"] == 2
    assert by_item["FACE WASH 100ML"]["to_create"] == 0
    assert by_item["FACE WASH 100ML"]["note"].startswith("MISMATCH")
    assert (by_item["SUGAR 500GM"]["tally_quantity"], by_item["SUGAR 500GM"]["already_held"]) == (0, 2)
    assert sum(line["to_create"] for line in later["lines"]) == 0


def test_stale_preview_token_is_refused(db_session):
    node = create_franchise_node(db_session, code="CHN-02", name="Chintamani", location="Chintamani")
    node.tally_godown_name = GODOWN
    db_session.commit()
    plan = godown_qr.plan_from_stock(db_session, node, {"FACE WASH 100ML": (1.0, "PCS")}, _items(), [])
    try:
        godown_qr.create_from_plan(db_session, node, plan, "other-token", created_by_id=None)
    except ValueError as exc:
        assert "out of date" in str(exc)
    else:
        raise AssertionError("stale token accepted")
