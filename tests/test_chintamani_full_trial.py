"""Full setup trial for one franchise, end to end through the node API.

Tally godown stock -> Master QR codes -> Lite command poll and acknowledgement
-> Lite stock-in, full sale and a return -> Master inventory -> Tally vouchers.
Tally itself is replaced by a stub; nothing is sent to a real company.
"""

from sqlalchemy import select

from app.models import Batch, BatchStatus, NetworkStock, Serial, Setting
from app.services import tally as tally_service
from app.services import tally_godown_qr as godown_qr
from app.services.tally import TallyResult, sync_batch
from app.services.tally_masters import TallyStockItem
from tests.test_network_sync import (  # noqa: F401 - api is a pytest fixture
    api,
    auth,
    create_node_with_key,
    event,
    item,
    post,
    seed_master_qr,
)
from tests.test_tally import VALID_SETTINGS

GODOWN = "CHINTHAMANI WAREHOUSE"
SALE_TYPE = "Chintamani Warehouse Sale"
RETURN_TYPE = "CREDIT NOTE CHINTAMANI WAREHOUSE"
TALLY_ITEMS = [
    TallyStockItem(name="SG PAPAYA FACE WASH 100ML", unit="PCS", hsn="3304", gst_rate=18, standard_rate=120),
    TallyStockItem(name="BGL SUGAR 500GM", unit="KG", hsn="1701", gst_rate=5, standard_rate=30),
]
TALLY_STOCK = {"SG PAPAYA FACE WASH 100ML": (3.0, "PCS"), "BGL SUGAR 500GM": (1.0, "KG")}


def _lite_item(serial: dict, product: dict, status: str) -> dict:
    return {
        "serial_number": serial["serial_number"],
        "product_code": product["product_code"],
        "product_name": product["product_name"],
        "tally_stock_item_name": product["tally_stock_item_name"],
        "hsn": product["hsn"],
        "gst_rate": product["gst_rate"],
        "unit": product["unit"],
        "rate": product["default_rate"],
        "status": status,
        "product_batch_number": serial["product_batch_number"],
        "warehouse": serial["warehouse"],
    }


def test_chintamani_stock_sales_and_tally_sync_round_trip(api, monkeypatch):  # noqa: F811
    client, db = api
    node, api_key = create_node_with_key(db, "CHN")
    node.tally_godown_name = GODOWN
    node.tally_sales_voucher_type = SALE_TYPE
    node.tally_sales_return_voucher_type = RETURN_TYPE
    db.commit()

    # 1. Master: QR codes from the Tally godown stock (3 pieces + 1 KG = 2 packets).
    plan = godown_qr.plan_from_stock(db, node, TALLY_STOCK, TALLY_ITEMS, [])
    assert godown_qr.create_from_plan(db, node, plan, plan["token"], created_by_id=None) == (2, 5)

    # 2. Lite: poll commands, apply them, acknowledge each one.
    response = client.get(
        "/api/v1/commands",
        headers={**auth(api_key), "X-Setuora-Command-Types": "PRODUCT_UPSERT,QR_ALLOCATED"},
    )
    assert response.status_code == 200, response.text
    commands = response.json()["data"]["commands"]
    assert sorted(c["type"] for c in commands) == ["PRODUCT_UPSERT"] * 2 + ["QR_ALLOCATED"] * 2
    received = []
    for command in commands:
        if command["type"] == "QR_ALLOCATED":
            product = command["payload"]["product"]
            received += [(serial, product) for serial in command["payload"]["serials"]]
        ack = client.patch(f"/api/v1/commands/{command['command_id']}", headers=auth(api_key))
        assert ack.status_code == 200, ack.text
    assert len(received) == 5
    again = client.get(
        "/api/v1/commands",
        headers={**auth(api_key), "X-Setuora-Command-Types": "PRODUCT_UPSERT,QR_ALLOCATED"},
    )
    assert again.json()["data"]["commands"] == []

    # 3. Lite: stock in, sell everything, take one return; events reach Master in order.
    stock_in = event(1, "PURCHASE", [_lite_item(s, p, "IN_STOCK") for s, p in received], party_name="Opening Stock")
    sale = event(2, "SALE", [_lite_item(s, p, "SOLD") for s, p in received], party_name="CASH CUSTOMER")
    returned_serial, returned_product = received[0]
    sales_return = event(
        3, "SALES_RETURN", [_lite_item(returned_serial, returned_product, "RETURNED")], party_name="CASH CUSTOMER"
    )
    response = post(client, api_key, stock_in, sale, sales_return)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["last_sequence"] == 3

    statuses = {
        serial.serial_number: (serial.status, stock.status, stock.current_franchise_id)
        for serial, stock in db.execute(
            select(Serial, NetworkStock).join(NetworkStock, NetworkStock.serial_id == Serial.id)
        )
    }
    # A returned piece goes back into sellable stock at the franchise.
    assert statuses[returned_serial["serial_number"]][:2] == ("IN_STOCK", "IN_STOCK")
    assert sum(1 for s in statuses.values() if s[0] == "SOLD") == 4
    assert all(s[2] == node.id for s in statuses.values())

    # 4. Master -> Tally: every voucher syncs once with the franchise's own types and godown.
    db.add_all(
        Setting(key=k, value=v)
        for k, v in {**VALID_SETTINGS, "tally_enabled": "true", "tally_host": "127.0.0.1", "tally_port": "9000"}.items()
    )
    db.commit()
    posted: list[str] = []

    def accepted(xml, _settings):
        posted.append(xml)
        return TallyResult(xml, "<RESPONSE><CREATED>1</CREATED></RESPONSE>", "CREATED=1")

    monkeypatch.setattr(tally_service, "post_to_tally", accepted)
    batches = db.scalars(select(Batch).order_by(Batch.id)).all()
    assert [b.batch_type for b in batches] == ["PURCHASE", "SALE", "SALES_RETURN"]
    for batch in batches:
        sync_batch(db, batch)
    assert [b.status for b in batches] == [BatchStatus.SYNCED.value] * 3

    # Stock-in of Tally opening stock is recorded but never posted as a Purchase.
    assert len(posted) == 2
    assert "opening stock already in Tally" in batches[0].notes
    sale_xml, return_xml = posted[0], posted[1]
    assert f"<VOUCHERTYPENAME>{SALE_TYPE}</VOUCHERTYPENAME>" in sale_xml
    assert f"<GODOWNNAME>{GODOWN}</GODOWNNAME>" in sale_xml
    assert "SG PAPAYA FACE WASH 100ML" in sale_xml and "BGL SUGAR 500GM" in sale_xml
    assert f"<VOUCHERTYPENAME>{RETURN_TYPE}</VOUCHERTYPENAME>" in return_xml
    # Two 500 g sugar packets leave Tally as 1 KG, not 2 KG; the billed amount is kept.
    assert "<ACTUALQTY>3 PCS</ACTUALQTY>" in sale_xml
    assert "<ACTUALQTY>1 KG</ACTUALQTY>" in sale_xml
    assert "<RATE>60.00/KG</RATE>" in sale_xml
    assert "<ACTUALQTY>2 KG</ACTUALQTY>" not in sale_xml

    # A second pass sends nothing more.
    for batch in batches:
        sync_batch(db, batch)
    assert len(posted) == 2
    assert not any("<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>" in xml for xml in posted)


def test_mixed_stock_in_posts_only_new_pieces_as_purchase(api):  # noqa: F811
    client, db = api
    node, api_key = create_node_with_key(db, "MIX")
    node.tally_godown_name = GODOWN
    db.commit()
    stock = {"SG PAPAYA FACE WASH 100ML": (2.0, "PCS")}
    plan = godown_qr.plan_from_stock(db, node, stock, TALLY_ITEMS[:1], [])
    godown_qr.create_from_plan(db, node, plan, plan["token"], created_by_id=None)
    commands = client.get(
        "/api/v1/commands",
        headers={**auth(api_key), "X-Setuora-Command-Types": "PRODUCT_UPSERT,QR_ALLOCATED"},
    ).json()["data"]["commands"]
    opening = [
        _lite_item(serial, command["payload"]["product"], "IN_STOCK")
        for command in commands
        if command["type"] == "QR_ALLOCATED"
        for serial in command["payload"]["serials"]
    ]
    assert len(opening) == 2
    new_piece = item("NEW-PIECE-1")
    seed_master_qr(db, node, new_piece)

    response = post(client, api_key, event(1, "PURCHASE", [*opening, new_piece], party_name="SUPPLIER"))

    assert response.status_code == 200, response.text
    batch = db.scalars(select(Batch).where(Batch.batch_type == "PURCHASE")).one()
    assert batch.status == BatchStatus.PENDING_SYNC.value
    assert [entry.serial.serial_number for entry in batch.items] == ["NEW-PIECE-1"]
    assert "2 of 3 pieces are Tally opening stock" in batch.notes


def test_opening_stock_booked_in_as_receive_posts_no_purchase(api):  # noqa: F811
    client, db = api
    node, api_key = create_node_with_key(db, "RCV")
    node.tally_godown_name = GODOWN
    db.commit()
    plan = godown_qr.plan_from_stock(
        db, node, {"SG PAPAYA FACE WASH 100ML": (2.0, "PCS")}, TALLY_ITEMS[:1], []
    )
    godown_qr.create_from_plan(db, node, plan, plan["token"], created_by_id=None)
    commands = client.get(
        "/api/v1/commands",
        headers={**auth(api_key), "X-Setuora-Command-Types": "PRODUCT_UPSERT,QR_ALLOCATED"},
    ).json()["data"]["commands"]
    opening = [
        _lite_item(serial, command["payload"]["product"], "IN_STOCK")
        for command in commands
        if command["type"] == "QR_ALLOCATED"
        for serial in command["payload"]["serials"]
    ]

    # Lite's stock-in screen can book QR stock as RECEIVE as well as PURCHASE.
    response = post(client, api_key, event(1, "RECEIVE", opening, party_name="CHINTHAMANI"))

    assert response.status_code == 200, response.text
    batch = db.scalars(select(Batch).where(Batch.batch_type == "RECEIVE")).one()
    assert batch.status == BatchStatus.SYNCED.value
    assert batch.items == []
    assert "opening stock already in Tally" in batch.notes
