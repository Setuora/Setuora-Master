from uuid import uuid4

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Batch,
    BatchItem,
    InboundEvent,
    Product,
    Serial,
    StorageLocation,
    User,
    utc_now,
)
from app.services.node_auth import create_franchise_node
from app.services.schema import _rebuild_sqlite_inventory_tables, ensure_runtime_schema


def test_existing_network_stock_allows_master_allocations_after_migration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old-network-stock.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        node = create_franchise_node(db, code="MIG-QR", name="Migration QR", location="Main")
        product = Product(
            product_code="MIG-QR:PROD",
            product_name="Migration product",
            hsn="1234",
            gst_rate=5,
            unit="Pcs",
            default_rate=10,
            tally_stock_item_name="Migration product",
        )
        db.add(product)
        db.flush()
        old_serial = Serial(serial_number="OLD-QR", product_id=product.id, status="IN_STOCK")
        new_serial = Serial(serial_number="SQR-00000000000000000000000000000001", product_id=product.id)
        inbound = InboundEvent(
            event_id=str(uuid4()),
            franchise_id=node.id,
            sequence=1,
            schema_version=1,
            event_type="STOCK_SNAPSHOT",
            occurred_at=utc_now(),
            payload_json="{}",
            payload_hash="old",
            result_json="{}",
        )
        db.add_all([old_serial, new_serial, inbound])
        db.commit()
        old_serial_id, new_serial_id, inbound_id, node_id = (
            old_serial.id,
            new_serial.id,
            inbound.id,
            node.id,
        )

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE network_stock"))
        connection.execute(
            text(
                "CREATE TABLE network_stock ("
                "id INTEGER PRIMARY KEY, serial_id INTEGER NOT NULL UNIQUE REFERENCES serials(id), "
                "current_franchise_id INTEGER NOT NULL REFERENCES franchise_nodes(id), "
                "origin_franchise_id INTEGER NOT NULL REFERENCES franchise_nodes(id), "
                "status VARCHAR(40) NOT NULL, "
                "last_event_id INTEGER NOT NULL REFERENCES inbound_events(id), "
                "updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO network_stock "
                "(id, serial_id, current_franchise_id, origin_franchise_id, status, "
                "last_event_id, updated_at) "
                "VALUES (1, :serial_id, :node_id, :node_id, 'IN_STOCK', :event_id, :now)"
            ),
            {"serial_id": old_serial_id, "node_id": node_id, "event_id": inbound_id, "now": utc_now()},
        )

    ensure_runtime_schema(engine)

    event_column = next(
        column for column in inspect(engine).get_columns("network_stock")
        if column["name"] == "last_event_id"
    )
    assert event_column["nullable"] is True
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT serial_id FROM network_stock WHERE id=1")) == old_serial_id
        connection.execute(
            text(
                "INSERT INTO network_stock "
                "(serial_id, current_franchise_id, origin_franchise_id, status, "
                "last_event_id, updated_at) "
                "VALUES (:serial_id, :node_id, :node_id, 'GENERATED', NULL, :now)"
            ),
            {"serial_id": new_serial_id, "node_id": node_id, "now": utc_now()},
        )


def test_runtime_schema_adds_sale_gst_columns_to_batches(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-gst.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE batches (
                    id INTEGER PRIMARY KEY,
                    batch_number VARCHAR(80),
                    batch_type VARCHAR(40),
                    party_name VARCHAR(180),
                    user_id INTEGER
                )
                """
            )
        )

    ensure_runtime_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("batches")}
    assert {
        "audit_assignment_id",
        "party_state",
        "party_gst_registration_type",
        "party_gst_name",
        "party_gstin",
        "gst_treatment",
        "gst_cgst_rate",
        "gst_sgst_rate",
        "gst_igst_rate",
        "tally_stock_location",
    } <= columns


def test_runtime_schema_adds_product_alias_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-products.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE batches (
                    id INTEGER PRIMARY KEY,
                    batch_number VARCHAR(80),
                    batch_type VARCHAR(40),
                    user_id INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    product_code VARCHAR(80),
                    product_name VARCHAR(180),
                    hsn VARCHAR(40),
                    gst_rate FLOAT,
                    unit VARCHAR(40),
                    default_rate FLOAT,
                    tally_stock_item_name VARCHAR(180),
                    active BOOLEAN
                )
                """
            )
        )

    ensure_runtime_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("products")}
    assert {"nickname", "alternate_tally_stock_item_name", "purchase_qr_print_allowed"} <= columns


def test_runtime_schema_adds_tally_user_to_cached_sales_vouchers(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-tally-cache.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE batches (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                """
                CREATE TABLE tally_sales_voucher_cache (
                    id INTEGER PRIMARY KEY,
                    company_id INTEGER,
                    tally_company VARCHAR(220),
                    tally_company_key VARCHAR(220),
                    remote_id VARCHAR(500),
                    voucher_date VARCHAR(40),
                    voucher_number VARCHAR(120),
                    voucher_type VARCHAR(120),
                    party_ledger VARCHAR(220),
                    amount VARCHAR(80),
                    narration TEXT,
                    refreshed_at DATETIME
                )
                """
            )
        )

    ensure_runtime_schema(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("tally_sales_voucher_cache")
    }
    assert "tally_user" in columns


def test_inventory_table_rebuild_preserves_rows_and_adds_all_foreign_keys(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        user = User(username="admin", password_hash="x", role="admin")
        product = Product(
            product_code="MIG001",
            product_name="Migration Product",
            hsn="1",
            gst_rate=5,
            unit="Pcs",
            default_rate=10,
            tally_stock_item_name="Migration Product",
        )
        location = StorageLocation(
            code="MIG-A-1",
            warehouse="MAIN",
            zone="A",
            section="1",
            rack="R1",
            shelf="S1",
            bin="B1",
        )
        db.add_all([user, product, location])
        db.flush()
        serial = Serial(
            serial_number="MIG001-000001",
            product_id=product.id,
            status="IN_STOCK",
            label_printed_by_id=user.id,
            location_id=location.id,
        )
        batch = Batch(
            batch_number="SAL-MIG-0001",
            batch_type="SALE",
            user_id=user.id,
        )
        db.add_all([serial, batch])
        db.flush()
        db.add(
            BatchItem(
                batch_id=batch.id,
                serial_id=serial.id,
                shelf_location_id=location.id,
                shelf_verified_by_id=user.id,
                sales_discount_rate=10,
            )
        )
        db.commit()

    _rebuild_sqlite_inventory_tables(engine)

    inspector = inspect(engine)
    serial_foreign_keys = {
        column
        for key in inspector.get_foreign_keys("serials")
        for column in key["constrained_columns"]
    }
    item_foreign_keys = {
        column
        for key in inspector.get_foreign_keys("batch_items")
        for column in key["constrained_columns"]
    }
    transaction_targets = {
        key["referred_table"] for key in inspector.get_foreign_keys("inventory_transactions")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM serials")) == 1
        assert connection.scalar(text("SELECT count(*) FROM batch_items")) == 1
        assert connection.scalar(text("SELECT sales_discount_rate FROM batch_items")) == 10
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    assert {
        "product_id",
        "replaced_by_id",
        "label_printed_by_id",
        "location_id",
    } <= serial_foreign_keys
    assert {
        "batch_id",
        "serial_id",
        "shelf_location_id",
        "shelf_verified_by_id",
    } <= item_foreign_keys
    assert "serials" in transaction_targets
