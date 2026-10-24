import json
from datetime import timedelta
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import (
    NetworkStock,
    NodeCommand,
    Product,
    ProductIdentityHistory,
    Serial,
    utc_now,
)
from app.services import tally_masters, tally_products
from app.services.network_ingest import _namespaced_code
from app.services.settings import add_company, update_settings
from app.services.tally_masters import TallyStockItem
from app.services.tally_products import (
    apply_tally_stock_items,
    import_blocker,
    import_tally_products,
    suggest_product_code,
    sync_is_due,
)
from tests import test_network_sync
from tests.test_network_sync import (
    auth,
    create_node_with_key,
    event,
    item,
    post,
    seed_master_qr,
)

api = test_network_sync.api  # shared Node API fixture

STOCK_ITEM_XML = """
<ENVELOPE><BODY><DATA><COLLECTION>
  <STOCKITEM NAME="Basmati Rice 5kg">
    <GUID>guid-rice</GUID>
    <PARENT>Grocery</PARENT>
    <BASEUNITS>Nos</BASEUNITS>
    <LANGUAGENAME.LIST><NAME.LIST TYPE="String"><NAME>Basmati Rice 5kg</NAME><NAME>RICE5</NAME></NAME.LIST></LANGUAGENAME.LIST>
    <HSNDETAILS.LIST><APPLICABLEFROM>20170701</APPLICABLEFROM><HSNCODE>1006</HSNCODE></HSNDETAILS.LIST>
    <GSTDETAILS.LIST>
      <APPLICABLEFROM>20170701</APPLICABLEFROM>
      <STATEWISEDETAILS.LIST>
        <RATEDETAILS.LIST><GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD><GSTRATE> 12</GSTRATE></RATEDETAILS.LIST>
      </STATEWISEDETAILS.LIST>
    </GSTDETAILS.LIST>
    <GSTDETAILS.LIST>
      <APPLICABLEFROM>20240401</APPLICABLEFROM>
      <STATEWISEDETAILS.LIST>
        <RATEDETAILS.LIST><GSTRATEDUTYHEAD>Central Tax</GSTRATEDUTYHEAD><GSTRATE> 2.5</GSTRATE></RATEDETAILS.LIST>
        <RATEDETAILS.LIST><GSTRATEDUTYHEAD>State Tax</GSTRATEDUTYHEAD><GSTRATE> 2.5</GSTRATE></RATEDETAILS.LIST>
        <RATEDETAILS.LIST><GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD><GSTRATE> 5</GSTRATE></RATEDETAILS.LIST>
      </STATEWISEDETAILS.LIST>
    </GSTDETAILS.LIST>
    <STANDARDPRICELIST.LIST><DATE>20230401</DATE><RATE>400.00/Nos</RATE></STANDARDPRICELIST.LIST>
    <STANDARDPRICELIST.LIST><DATE>20240401</DATE><RATE>1,250.50/Nos</RATE></STANDARDPRICELIST.LIST>
  </STOCKITEM>
  <STOCKITEM NAME="Loose Item">
    <GUID>guid-loose</GUID>
    <PARENT>Misc</PARENT>
  </STOCKITEM>
</COLLECTION></DATA></BODY></ENVELOPE>
"""

COMPANY_CONFIG = {
    "company_name": "Central Co",
    "tally_host": "127.0.0.1",
    "tally_port": "9000",
    "sales_voucher_type": "Sales",
    "purchase_voucher_type": "Purchase",
    "sales_ledger_name": "Sales",
    "purchase_ledger_name": "Purchase",
    "cgst_ledger_name": "CGST",
    "sgst_ledger_name": "SGST",
    "sales_gst_ledger_mappings": "",
    "round_off_ledger_name": "Round Off",
}


def stock_item(name="Basmati Rice 5kg", **values) -> TallyStockItem:
    defaults = {"guid": "guid-rice", "unit": "Nos", "hsn": "1006", "gst_rate": 5.0}
    defaults.update(values)
    return TallyStockItem(name=name, **defaults)


def test_stock_item_export_reads_current_hsn_gst_rate_and_alias(monkeypatch):
    requests = []

    def fake_post(settings, xml, *, timeout=8):
        requests.append((xml, timeout))
        return xml, ET.fromstring(STOCK_ITEM_XML)

    monkeypatch.setattr(tally_masters, "_post_read_request", fake_post)

    items = tally_masters.fetch_tally_stock_items({}, "Central Co")

    assert [entry.name for entry in items] == ["Basmati Rice 5kg", "Loose Item"]
    rice, loose = items
    assert rice == TallyStockItem(
        name="Basmati Rice 5kg",
        guid="guid-rice",
        parent="Grocery",
        unit="Nos",
        hsn="1006",
        gst_rate=5.0,
        standard_rate=1250.5,
        alias="RICE5",
    )
    assert loose.gst_rate is None
    assert "<TYPE>StockItem</TYPE>" in requests[0][0]
    assert "<SVCURRENTCOMPANY>Central Co</SVCURRENTCOMPANY>" in requests[0][0]
    assert requests[0][1] == tally_masters.STOCK_ITEM_READ_TIMEOUT_SECONDS
    assert suggest_product_code(rice) == "RICE5"
    assert suggest_product_code(stock_item("Dal / Toor 1kg", alias="Toor dal")) == "DAL-TOOR-1KG"
    assert import_blocker(loose) == "Set the GST rate on this stock item in Tally."


def test_import_creates_products_per_franchise_and_queues_product_commands(api):
    _client, db = api
    first, _ = create_node_with_key(db, "IMP1")
    second, _ = create_node_with_key(db, "IMP2")
    db.add(
        Product(
            product_code=_namespaced_code(second, "OLD-RICE"),
            product_name="Rice (manual)",
            tally_stock_item_name="Basmati Rice 5kg",
            hsn="1006",
            gst_rate=5,
            unit="Nos",
        )
    )
    db.commit()

    summary = import_tally_products(
        db,
        nodes=[first, second],
        items=[stock_item(alias="RICE5", standard_rate=99.0), stock_item("Loose", gst_rate=None)],
    )
    db.commit()

    assert (summary.created, summary.existing, summary.skipped) == (1, 1, 2)
    created = db.scalar(select(Product).where(Product.product_code == "FIMP1:RICE5"))
    assert created.default_rate == 99.0
    assert created.tally_guid == "guid-rice"
    assert created.tally_synced_at is not None
    linked = db.scalar(select(Product).where(Product.product_code == "FIMP2:OLD-RICE"))
    assert linked.tally_synced_at is not None

    commands = db.scalars(select(NodeCommand)).all()
    assert [(command.target_franchise_id, command.command_type) for command in commands] == [
        (first.id, "PRODUCT_UPSERT")
    ]
    payload = json.loads(commands[0].payload_json)
    assert payload["franchise_code"] == "FIMP1"
    assert payload["product"]["product_code"] == "RICE5"
    assert payload["product"]["gst_rate"] == 5.0


def test_import_reuses_the_code_of_stock_received_by_transfer(api):
    _client, db = api
    origin, _ = create_node_with_key(db, "IMP3")
    destination, _ = create_node_with_key(db, "IMP4")
    product = Product(
        product_code=_namespaced_code(origin, "P100"),
        product_name="Basmati Rice 5kg",
        tally_stock_item_name="Basmati Rice 5kg",
        hsn="1006",
        gst_rate=5,
        unit="Nos",
    )
    db.add(product)
    db.flush()
    serial = Serial(serial_number="SQR-TRANSFERRED-1", product_id=product.id, status="IN_STOCK")
    db.add(serial)
    db.flush()
    db.add(
        NetworkStock(
            serial_id=serial.id,
            current_franchise_id=destination.id,
            origin_franchise_id=origin.id,
            status="IN_STOCK",
        )
    )
    db.commit()

    summary = import_tally_products(db, nodes=[destination], items=[stock_item(alias="RICE5")])
    db.commit()

    assert summary.created == 1
    assert db.scalar(
        select(Product.id).where(Product.product_code == _namespaced_code(destination, "P100"))
    )
    command = db.scalar(select(NodeCommand).where(NodeCommand.target_franchise_id == destination.id))
    assert json.loads(command.payload_json)["product"]["product_code"] == "P100"


def test_product_commands_wait_until_lite_declares_support(api):
    client, db = api
    node, api_key = create_node_with_key(db, "GATE")
    import_tally_products(db, nodes=[node], items=[stock_item(alias="RICE5")])
    db.commit()

    old_lite = client.get("/api/v1/commands", headers=auth(api_key))
    assert old_lite.status_code == 200
    assert old_lite.json()["data"]["commands"] == []

    new_lite = client.get(
        "/api/v1/commands",
        headers={**auth(api_key), "X-Setuora-Command-Types": "PRODUCT_UPSERT"},
    )
    commands = new_lite.json()["data"]["commands"]
    assert [command["type"] for command in commands] == ["PRODUCT_UPSERT"]


def test_live_sync_updates_identity_and_accepts_events_frozen_before_the_change(api):
    client, db = api
    node, api_key = create_node_with_key(db, "SYNC")
    stock = item("QR-LIVE-1", product_code="RICE5")
    stock.update(
        {
            "product_name": "Basmati Rice 5kg",
            "tally_stock_item_name": "Basmati Rice 5kg",
            "hsn": "1006",
            "gst_rate": 5,
            "unit": "Nos",
        }
    )
    seed_master_qr(db, node, stock)
    product = db.scalar(select(Product).where(Product.product_code == "FSYNC:RICE5"))
    product.tally_guid = "guid-rice"
    product.tally_synced_at = utc_now()
    db.commit()

    summary = apply_tally_stock_items(
        db, [stock_item("Basmati Rice Premium 5kg", hsn="10063", gst_rate=12.0)]
    )
    db.commit()

    assert (summary.checked, summary.updated, summary.missing) == (1, 1, 0)
    db.refresh(product)
    assert product.product_name == "Basmati Rice Premium 5kg"
    assert product.tally_stock_item_name == "Basmati Rice Premium 5kg"
    assert (product.hsn, product.gst_rate) == ("10063", 12.0)
    history = db.scalars(select(ProductIdentityHistory)).all()
    assert [(row.tally_stock_item_name, row.hsn, row.gst_rate) for row in history] == [
        ("Basmati Rice 5kg", "1006", 5.0)
    ]
    command = db.scalar(select(NodeCommand).where(NodeCommand.command_type == "PRODUCT_UPSERT"))
    assert json.loads(command.payload_json)["product"]["hsn"] == "10063"

    # Lite queued this purchase with the old identity before it saw the command.
    accepted = post(client, api_key, event(1, "PURCHASE", [stock], party_name="Supplier"))
    assert accepted.status_code == 200, accepted.text

    stranger = item("QR-LIVE-2", product_code="RICE5")
    stranger.update({"tally_stock_item_name": "Something else", "unit": "Nos", "hsn": "1006"})
    seed_master_qr(db, node, {**stranger, "serial_number": "QR-LIVE-2"})
    rejected = post(client, api_key, event(2, "PURCHASE", [stranger], party_name="Supplier"))
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "PRODUCT_IDENTITY_CONFLICT"


def test_live_sync_counts_missing_items_and_leaves_unchanged_products_quiet(db_session):
    db_session.add_all(
        [
            Product(
                product_code="F1:RICE5",
                product_name="Shelf name",
                tally_stock_item_name="Basmati Rice 5kg",
                hsn="1006",
                gst_rate=5,
                unit="Nos",
                tally_synced_at=utc_now(),
            ),
            Product(
                product_code="F1:GONE",
                product_name="Gone",
                tally_stock_item_name="Deleted in Tally",
                hsn="1",
                gst_rate=5,
                unit="Nos",
                tally_synced_at=utc_now(),
            ),
            Product(
                product_code="F1:MANUAL",
                product_name="Manual",
                tally_stock_item_name="Manual",
                hsn="1",
                gst_rate=5,
                unit="Nos",
            ),
        ]
    )
    db_session.commit()

    summary = apply_tally_stock_items(db_session, [stock_item()])

    assert (summary.checked, summary.updated, summary.missing) == (2, 0, 1)
    assert db_session.scalars(select(ProductIdentityHistory)).all() == []
    assert db_session.scalars(select(NodeCommand)).all() == []


def test_sync_is_due_only_with_linked_products_and_after_the_interval(db_session):
    assert sync_is_due(db_session) is False
    db_session.add(
        Product(
            product_code="F1:RICE5",
            product_name="Rice",
            tally_stock_item_name="Rice",
            hsn="1006",
            gst_rate=5,
            unit="Nos",
            tally_synced_at=utc_now(),
        )
    )
    db_session.commit()
    assert sync_is_due(db_session) is True

    now = utc_now()
    update_settings(db_session, {tally_products.SYNC_LAST_AT_KEY: now.isoformat()})
    assert sync_is_due(db_session, now + timedelta(minutes=14)) is False
    assert sync_is_due(db_session, now + timedelta(minutes=15)) is True

    tally_products.save_sync_interval(db_session, "0")
    assert sync_is_due(db_session, now + timedelta(days=1)) is False


def test_run_sync_records_failures_and_results(db_session, monkeypatch):
    sessions = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(tally_products, "SessionLocal", sessions)
    add_company(db_session, "Central", COMPANY_CONFIG)

    def failing_fetch(settings, company):
        raise tally_masters.TallyDataError("Tally gateway timed out.")

    monkeypatch.setattr(tally_products, "fetch_tally_stock_items", failing_fetch)
    try:
        tally_products.run_tally_product_sync()
    except tally_masters.TallyDataError:
        pass
    else:  # pragma: no cover - the fetch above always fails
        raise AssertionError("expected the Tally failure to propagate")
    db_session.expire_all()
    from app.services.settings import get_setting

    assert get_setting(db_session, tally_products.SYNC_LAST_RESULT_KEY).startswith("Failed:")

    monkeypatch.setattr(tally_products, "fetch_tally_stock_items", lambda settings, company: [])
    summary = tally_products.run_tally_product_sync()
    db_session.expire_all()
    assert summary.checked == 0
    assert get_setting(db_session, tally_products.SYNC_LAST_RESULT_KEY).startswith("Checked 0")


def test_products_pages_import_ticked_items_for_ticked_franchises(api, monkeypatch):
    from app.models import User
    from app.routers.tally_products import router as products_router
    from tests.factories import authenticate_client

    client, db = api
    client.app.include_router(products_router)
    node, _ = create_node_with_key(db, "WEB")
    create_node_with_key(db, "SKIP")
    add_company(db, "Central", COMPANY_CONFIG)
    admin = User(username="products-admin", password_hash="x", role="admin")
    viewer = User(username="products-viewer", password_hash="x", role="directors")
    db.add_all([admin, viewer])
    db.commit()
    monkeypatch.setattr(
        "app.routers.tally_products.fetch_tally_stock_items",
        lambda settings, company: [
            stock_item(alias="RICE5"),
            stock_item("Sugar 1kg", guid="guid-sugar", hsn="1701"),
            stock_item("Loose", guid="guid-loose", gst_rate=None),
        ],
    )

    authenticate_client(client, viewer.id)
    assert client.get("/network/products", follow_redirects=False).status_code != 200

    authenticate_client(client, admin.id)
    page = client.get("/network/products")
    assert page.status_code == 200
    assert "Live sync with Tally" in page.text
    assert "Every 15 minutes" in page.text

    picker = client.get("/network/products/import")
    assert picker.status_code == 200
    assert 'name="item" value="Basmati Rice 5kg"' in picker.text
    assert "Set the GST rate on this stock item in Tally." in picker.text
    assert f'value="{node.public_id}"' in picker.text

    missing_franchise = client.post(
        "/network/products/import", data={"item": ["Basmati Rice 5kg"]}, follow_redirects=False
    )
    assert "Tick+at+least+one+active+franchise" in missing_franchise.headers["location"]

    imported = client.post(
        "/network/products/import",
        data={"item": ["Basmati Rice 5kg", "Gone from Tally"], "franchise": [node.public_id]},
    )
    assert imported.status_code == 200
    assert "Imported 1 product; 1 are no longer in Tally." in imported.text
    codes = db.scalars(select(Product.product_code).order_by(Product.product_code)).all()
    assert codes == ["FWEB:RICE5"]

    saved = client.post("/network/products/sync-settings", data={"sync_minutes": "45"})
    assert "Live sync runs every 45 minutes." in saved.text
    rejected = client.post("/network/products/sync-settings", data={"sync_minutes": "-1"})
    assert "Live sync interval must be 0 to 1440 minutes." in rejected.text
