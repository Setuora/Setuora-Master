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


def test_item_request_passes_the_name_as_a_variable_not_a_formula():
    xml = godown_qr._item_batches_xml("Company & Co", '14" Cut Off Wheel')
    root = fromstring(xml)
    assert root.find(".//COLLECTION/CHILDOF").text == "##SetuoraItem"
    assert root.find(".//STATICVARIABLES/SETUORAITEM").text == '14" Cut Off Wheel'
    assert root.find(".//TDLMESSAGE/VARIABLE").get("NAME") == "SetuoraItem"


def _reply(body: str):
    return "", fromstring(f"<ENVELOPE><BODY><DATA><COLLECTION>{body}</COLLECTION></DATA></BODY></ENVELOPE>")


def test_fetch_reads_only_the_franchise_godown_including_quoted_names(monkeypatch):
    requests = []
    batches = {
        "SUGAR 500GM": (
            '<BATCH NAME="Primary Batch"><GODOWNNAME>CHINTHAMANI WAREHOUSE</GODOWNNAME>'
            "<CLOSINGBALANCE> 15.00 KG</CLOSINGBALANCE></BATCH>"
            '<BATCH NAME="Primary Batch"><GODOWNNAME>Main Location</GODOWNNAME>'
            "<CLOSINGBALANCE> 99.00 KG</CLOSINGBALANCE></BATCH>"
        ),
        '14" Cut Off Wheel': (
            '<BATCH NAME="Primary Batch"><GODOWNNAME>CHINTHAMANI WAREHOUSE</GODOWNNAME>'
            "<CLOSINGBALANCE> 3.00 PCS</CLOSINGBALANCE></BATCH>"
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
        item = fromstring(xml).find(".//STATICVARIABLES/SETUORAITEM")
        if item is not None and item.text in batches:
            return _reply(batches[item.text])
        raise AssertionError("unexpected request")

    monkeypatch.setattr(godown_qr, "_post_read_request", fake_post)
    monkeypatch.setattr(godown_qr, "ITEM_PAUSE_SECONDS", 0)

    quantities, skipped = godown_qr.fetch_godown_quantities({}, "Company", GODOWN)

    assert quantities == {"SUGAR 500GM": (15.0, "KG"), '14" Cut Off Wheel': (3.0, "PCS")}
    assert skipped == []
    assert len(requests) == 4  # the list, then only the three items with stock


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


def test_reload_counts_sales_not_yet_posted_to_tally_as_held(db_session):
    from app.models import Batch, BatchItem, BatchStatus, BatchType, SerialStatus

    node = create_franchise_node(db_session, code="CHN-04", name="Chintamani", location="Chintamani")
    node.tally_godown_name = GODOWN
    db_session.commit()
    plan = godown_qr.plan_from_stock(db_session, node, {"FACE WASH 100ML": (3.0, "PCS")}, _items(), [])
    godown_qr.create_from_plan(db_session, node, plan, plan["token"], created_by_id=None)
    serial = db_session.scalars(select(Serial).order_by(Serial.id)).first()
    stock = db_session.scalar(select(NetworkStock).where(NetworkStock.serial_id == serial.id))
    serial.status = stock.status = SerialStatus.SOLD.value
    sale = Batch(
        batch_number="NET-SALE-1",
        batch_type=BatchType.SALE.value,
        status=BatchStatus.PENDING_SYNC.value,
        user_id=node.service_user_id,
    )
    db_session.add(sale)
    db_session.flush()
    db_session.add(BatchItem(batch_id=sale.id, serial_id=serial.id, quantity=1, rate=120))
    db_session.commit()

    # Tally still shows 3 until the sale posts: nothing new to create.
    line = godown_qr.plan_from_stock(db_session, node, {"FACE WASH 100ML": (3.0, "PCS")}, _items(), [])["lines"][0]
    assert (line["already_held"], line["to_create"]) == (3, 0)

    # Once posted, Tally shows 2 and Master holds 2.
    sale.status = BatchStatus.SYNCED.value
    db_session.commit()
    line = godown_qr.plan_from_stock(db_session, node, {"FACE WASH 100ML": (2.0, "PCS")}, _items(), [])["lines"][0]
    assert (line["already_held"], line["to_create"]) == (2, 0)


class _Use:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


def _loaded_node(db_session, code, pieces=4):
    node = create_franchise_node(db_session, code=code, name=f"N {code}", location="X")
    node.tally_godown_name = GODOWN
    db_session.commit()
    plan = godown_qr.plan_from_stock(
        db_session, node, {"FACE WASH 100ML": (float(pieces), "PCS")}, _items(), []
    )
    godown_qr.create_from_plan(db_session, node, plan, plan["token"], created_by_id=None)
    return node


def _set_status(db_session, serial, status):
    stock = db_session.scalar(select(NetworkStock).where(NetworkStock.serial_id == serial.id))
    serial.status = stock.status = status
    return stock


def test_held_count_is_what_tally_still_shows_in_the_godown(db_session):
    from app.models import Batch, BatchItem, BatchStatus, BatchType, SerialStatus

    node = _loaded_node(db_session, "CHN-05", pieces=5)
    other = create_franchise_node(db_session, code="CHN-06", name="Other", location="Y")
    damaged, issued, in_transit, moved, sold = db_session.scalars(
        select(Serial).order_by(Serial.id)
    ).all()
    _set_status(db_session, damaged, SerialStatus.DAMAGED.value)  # credit note put it back in Tally
    _set_status(db_session, issued, SerialStatus.ISSUED.value)  # never posted out of Tally
    _set_status(db_session, in_transit, SerialStatus.IN_TRANSIT.value)
    _set_status(db_session, moved, SerialStatus.IN_STOCK.value).current_franchise_id = other.id
    _set_status(db_session, sold, SerialStatus.SOLD.value)
    journal = Batch(
        batch_number="NET-SJ-1",
        batch_type=BatchType.STOCK_TRANSFER.value,
        status=BatchStatus.PENDING_SYNC.value,
        user_id=node.service_user_id,
        tally_source_location=GODOWN.lower(),
    )
    sale = Batch(
        batch_number="NET-S-1",
        batch_type=BatchType.SALE.value,
        status=BatchStatus.SYNCED.value,
        user_id=node.service_user_id,
    )
    db_session.add_all([journal, sale])
    db_session.flush()
    db_session.add_all(
        [
            BatchItem(batch_id=journal.id, serial_id=moved.id, quantity=1, rate=1),
            BatchItem(batch_id=sale.id, serial_id=sold.id, quantity=1, rate=1),
        ]
    )
    db_session.commit()

    # Only the posted sale has left Tally's godown.
    assert sum(godown_qr._held_counts(db_session, node).values()) == 4
    journal.status = BatchStatus.SYNCED.value
    db_session.commit()
    assert sum(godown_qr._held_counts(db_session, node).values()) == 3


def test_auto_sync_labels_new_stock_only_for_loaded_franchises(db_session, monkeypatch):
    _loaded_node(db_session, "CHN-07", pieces=4)
    fresh = create_franchise_node(db_session, code="CHN-08", name="Fresh", location="Z")
    fresh.tally_godown_name = "FRESH GODOWN"
    db_session.commit()
    reads = []

    def read(config, company, godown):
        reads.append(godown)
        return {"FACE WASH 100ML": (6.0, "PCS")}, _items()[:1], []

    monkeypatch.setattr(godown_qr, "SessionLocal", lambda: _Use(db_session))
    monkeypatch.setattr(godown_qr, "active_tally_source", lambda db: ({}, "Company"))
    monkeypatch.setattr(godown_qr, "read_godown_stock", read)

    result = godown_qr.run_godown_auto_sync()

    assert reads == [GODOWN]  # the franchise without a first load is left alone
    assert "CHN-07: 2 new QR labels for 1 items" in result
    assert db_session.scalar(select(func.count(Serial.id))) == 6
    # Not due again until the interval passes; then nothing new is created.
    assert godown_qr.run_godown_auto_sync() is None
    assert "0 new QR labels" in godown_qr.run_godown_auto_sync(force=True)
    assert db_session.scalar(select(func.count(Serial.id))) == 6


def test_auto_sync_waits_when_a_voucher_posts_during_the_read(db_session, monkeypatch):
    from app.models import Batch, BatchStatus, BatchType, utc_now

    node = _loaded_node(db_session, "CHN-09", pieces=4)

    def read(config, company, godown):
        db_session.add(
            Batch(
                batch_number="NET-S-9",
                batch_type=BatchType.SALE.value,
                status=BatchStatus.SYNCED.value,
                user_id=node.service_user_id,
                synced_at=utc_now(),
            )
        )
        db_session.commit()
        return {"FACE WASH 100ML": (9.0, "PCS")}, _items()[:1], []

    monkeypatch.setattr(godown_qr, "SessionLocal", lambda: _Use(db_session))
    monkeypatch.setattr(godown_qr, "active_tally_source", lambda db: ({}, "Company"))
    monkeypatch.setattr(godown_qr, "read_godown_stock", read)

    assert "checking again next run" in godown_qr.run_godown_auto_sync(force=True)
    assert db_session.scalar(select(func.count(Serial.id))) == 4
