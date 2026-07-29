from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.middleware import NodeAPIBodyLimitMiddleware
from app.models import (
    Batch,
    BatchStatus,
    InventoryTransaction,
    NetworkStock,
    NodeCommand,
    NodeCredential,
    Serial,
    StockTransfer,
    TransferStatus,
)
from app.routers.master_console import _movement_rows
from app.routers.node_api import router as node_api_router
from app.services.node_auth import (
    clear_node_rate_limits,
    create_franchise_node,
    parse_api_key,
    provision_node_credential,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC).isoformat()


def item(serial_number: str, *, status: str = "IN_STOCK", product_code: str = "SKU-1"):
    return {
        "serial_number": serial_number,
        "product_code": product_code,
        "product_name": f"Product {product_code}",
        "tally_stock_item_name": f"Tally {product_code}",
        "hsn": "1234",
        "gst_rate": 18,
        "unit": "Pcs",
        "rate": 100,
        "status": status,
        "product_batch_number": "LOT-1",
        "mfg_date": "2026-01-01",
        "expiry_date": "2028-01-01",
        "warehouse": "MAIN",
    }


def event(
    sequence: int,
    event_type: str,
    items: list[dict] | None = None,
    *,
    event_id: str | None = None,
    **values,
):
    payload = {
        "event_id": event_id or str(uuid4()),
        "sequence": sequence,
        "schema_version": 1,
        "type": event_type,
        "occurred_at": NOW,
        "reference": f"REF-{sequence}",
        "actor": "lite-user",
        "items": items or [],
    }
    payload.update(values)
    return payload


@pytest.fixture()
def api():
    clear_node_rate_limits()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.add_middleware(NodeAPIBodyLimitMiddleware)
    app.include_router(node_api_router)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, session
    session.close()
    engine.dispose()
    clear_node_rate_limits()


def create_node_with_key(db, suffix: str):
    node = create_franchise_node(
        db,
        code=f"F{suffix}",
        name=f"FRANCHISE {suffix}",
        location=f"LOCATION {suffix}",
    )
    provisioned = provision_node_credential(db, node)
    return node, provisioned.api_key


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def post(api: TestClient, api_key: str, *events: dict):
    return api.post(
        "/api/v1/events",
        headers=auth(api_key),
        json={"events": list(events)},
    )


def test_credentials_store_only_a_hash_and_authenticate(api):
    api, db_session = api
    node, api_key = create_node_with_key(db_session, "A")
    parsed = parse_api_key(api_key)
    assert parsed is not None
    key_id, plaintext_secret = parsed

    credential = db_session.scalar(select(NodeCredential).where(NodeCredential.key_id == key_id))
    assert credential is not None
    assert len(credential.secret_hash) == 64
    assert credential.secret_hash != plaintext_secret
    assert plaintext_secret not in credential.secret_hash
    assert not hasattr(credential, "secret")

    missing = api.get("/api/v1/node")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTH_REQUIRED"

    invalid = api.get(
        "/api/v1/node",
        headers=auth(f"{api_key}wrong"),
    )
    assert invalid.status_code == 401

    response = api.get("/api/v1/node", headers=auth(api_key))
    assert response.status_code == 200
    assert response.json()["data"]["public_id"] == node.public_id
    assert response.json()["data"]["next_sequence"] == 1


def test_body_limit_middleware_uses_node_api_error_envelopes(api):
    api, _db_session = api

    missing_length_request = api.build_request(
        "POST",
        "/api/v1/events",
        content=(chunk for chunk in [b"{}"]),
    )
    missing_length = api.send(missing_length_request)
    assert missing_length.status_code == 411
    assert missing_length.json()["data"] is None
    assert missing_length.json()["error"]["code"] == "CONTENT_LENGTH_REQUIRED"
    assert missing_length.json()["request_id"] == missing_length.headers["x-request-id"]

    invalid_length = api.post(
        "/api/v1/events",
        headers={"content-length": "not-a-number", "x-request-id": "request-400"},
        content=b"{}",
    )
    assert invalid_length.status_code == 400
    assert invalid_length.json() == {
        "data": None,
        "error": {
            "code": "INVALID_CONTENT_LENGTH",
            "message": "Content-Length must be an integer.",
        },
        "request_id": "request-400",
    }
    assert invalid_length.headers["x-request-id"] == "request-400"

    too_large = api.post(
        "/api/v1/events",
        headers={"content-length": str(5 * 1024 * 1024 + 1)},
        content=b"{}",
    )
    assert too_large.status_code == 413
    assert too_large.json()["data"] is None
    assert too_large.json()["error"]["code"] == "BODY_TOO_LARGE"
    assert too_large.json()["request_id"] == too_large.headers["x-request-id"]


def test_event_idempotency_gap_conflicts_and_franchise_isolation(api):
    api, db_session = api
    first_node, first_key = create_node_with_key(db_session, "A")
    second_node, second_key = create_node_with_key(db_session, "B")
    purchase = event(
        1,
        "PURCHASE",
        [item("QR-IDEMPOTENT")],
        party_name="Supplier",
    )

    first = post(api, first_key, purchase)
    assert first.status_code == 200, first.text
    acknowledgement = first.json()["data"]["acknowledgements"][0]

    duplicate = post(api, first_key, purchase)
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["acknowledgements"][0] == acknowledgement

    changed = dict(purchase)
    changed["actor"] = "different-user"
    conflict = post(api, first_key, changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "EVENT_ID_CONFLICT"

    sequence_conflict = event(
        1,
        "PURCHASE",
        [item("QR-OTHER")],
        party_name="Supplier",
    )
    response = post(api, first_key, sequence_conflict)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SEQUENCE_CONFLICT"

    gap = event(3, "HEARTBEAT")
    response = post(api, first_key, gap)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SEQUENCE_GAP"
    assert response.json()["error"]["details"]["expected_sequence"] == 2

    cross_franchise = post(api, second_key, purchase)
    assert cross_franchise.status_code == 403
    assert cross_franchise.json()["error"]["code"] == "EVENT_FORBIDDEN"

    db_session.refresh(first_node)
    db_session.refresh(second_node)
    assert first_node.last_sequence == 1
    assert second_node.last_sequence == 0


def test_purchase_then_sale_mirrors_inventory_and_queues_tally(api):
    api, db_session = api
    node, api_key = create_node_with_key(db_session, "A")
    purchase = event(
        1,
        "PURCHASE",
        [item("QR-PURCHASE-SALE")],
        party_name="Supplier Ledger",
    )
    sale = event(
        2,
        "SALE",
        [item("QR-PURCHASE-SALE", status="SOLD")],
        party_name="Customer Ledger",
        party_gstin="29abcde1234f1z5",
    )

    response = post(api, api_key, purchase, sale)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["last_sequence"] == 2

    serial = db_session.scalar(select(Serial).where(Serial.serial_number == "QR-PURCHASE-SALE"))
    stock = db_session.scalar(select(NetworkStock).where(NetworkStock.serial_id == serial.id))
    assert serial.status == "SOLD"
    assert stock.status == "SOLD"
    assert stock.current_franchise_id == node.id

    batches = db_session.scalars(select(Batch).order_by(Batch.id)).all()
    assert [batch.batch_type for batch in batches] == ["PURCHASE", "SALE"]
    assert all(batch.status == BatchStatus.PENDING_SYNC.value for batch in batches)
    assert all(batch.sync_remote_id for batch in batches)
    assert batches[-1].party_gstin == "29ABCDE1234F1Z5"

    transactions = db_session.scalars(
        select(InventoryTransaction).order_by(InventoryTransaction.id)
    ).all()
    assert [row.transaction_type for row in transactions] == ["PURCHASE", "SALE"]
    assert transactions[-1].status_from == "IN_STOCK"
    assert transactions[-1].status_to == "SOLD"


def test_dispatch_partial_and_full_receipt_move_global_ownership(api):
    api, db_session = api
    source, source_key = create_node_with_key(db_session, "SOURCE")
    destination, destination_key = create_node_with_key(db_session, "DEST")
    first_item = item("QR-TRANSFER-1")
    second_item = item("QR-TRANSFER-2", product_code="SKU-2")

    purchase = event(
        1,
        "PURCHASE",
        [first_item, second_item],
        party_name="Supplier",
    )
    assert post(api, source_key, purchase).status_code == 200

    dispatch_id = str(uuid4())
    dispatch = event(
        2,
        "TRANSFER_DISPATCHED",
        [first_item, second_item],
        event_id=dispatch_id,
        reference="TRANSFER-100",
        destination_franchise_code=destination.code,
    )
    dispatched = post(api, source_key, dispatch)
    assert dispatched.status_code == 200, dispatched.text
    transfer_id = dispatched.json()["data"]["acknowledgements"][0]["result"]["transfer_id"]
    assert transfer_id == dispatch_id

    transfer = db_session.scalar(
        select(StockTransfer).where(StockTransfer.public_id == transfer_id)
    )
    assert transfer.status == TransferStatus.DISPATCHED.value
    assert all(link.serial.status == "IN_TRANSIT" for link in transfer.items)

    incoming = api.get("/api/v1/commands", headers=auth(destination_key))
    assert incoming.status_code == 200
    incoming_commands = incoming.json()["data"]["commands"]
    assert len(incoming_commands) == 1
    assert incoming_commands[0]["type"] == "TRANSFER_AVAILABLE"
    manifest = incoming_commands[0]["payload"]
    assert manifest["transfer"]["transfer_uuid"] == transfer_id
    assert manifest["transfer"]["source_franchise_code"] == source.code
    assert manifest["items"][0]["manifest_serial_number"] == "QR-TRANSFER-1"
    assert manifest["items"][0]["product"]["default_rate"] == 100

    first_receipt = event(
        1,
        "TRANSFER_RECEIVED",
        [first_item],
        transfer_id=transfer_id,
    )
    response = post(api, destination_key, first_receipt)
    assert response.status_code == 200, response.text
    assert (
        response.json()["data"]["acknowledgements"][0]["result"]["transfer_status"]
        == TransferStatus.PARTIALLY_RECEIVED.value
    )

    db_session.refresh(transfer)
    stocks = {
        row.serial.serial_number: row for row in db_session.scalars(select(NetworkStock)).all()
    }
    assert stocks["QR-TRANSFER-1"].current_franchise_id == destination.id
    assert stocks["QR-TRANSFER-1"].status == "IN_STOCK"
    assert stocks["QR-TRANSFER-2"].current_franchise_id == source.id
    assert stocks["QR-TRANSFER-2"].status == "IN_TRANSIT"

    second_receipt = event(
        2,
        "TRANSFER_RECEIVED",
        [second_item],
        transfer_id=transfer_id,
    )
    response = post(api, destination_key, second_receipt)
    assert response.status_code == 200, response.text
    assert (
        response.json()["data"]["acknowledgements"][0]["result"]["transfer_status"]
        == TransferStatus.RECEIVED.value
    )

    db_session.refresh(transfer)
    assert transfer.status == TransferStatus.RECEIVED.value
    assert all(link.received_quantity == 1 for link in transfer.items)
    assert all(
        row.current_franchise_id == destination.id
        for row in db_session.scalars(select(NetworkStock)).all()
    )
    report_rows = _movement_rows(db_session)
    assert (
        sum(int(row["transferred_out"]) for row in report_rows if row["franchise"].id == source.id)
        == 2
    )
    assert (
        sum(
            int(row["transferred_in"])
            for row in report_rows
            if row["franchise"].id == destination.id
        )
        == 2
    )

    receipt_commands = db_session.scalars(
        select(NodeCommand).where(NodeCommand.target_franchise_id == source.id)
    ).all()
    assert len(receipt_commands) == 2
    assert all(command.command_type == "TRANSFER_RECEIPT" for command in receipt_commands)

    wrong_node_ack = api.patch(
        f"/api/v1/commands/{incoming_commands[0]['command_id']}",
        headers=auth(source_key),
    )
    assert wrong_node_ack.status_code == 403

    acknowledged = api.patch(
        f"/api/v1/commands/{incoming_commands[0]['command_id']}",
        headers=auth(destination_key),
        json={"acknowledged": True},
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["data"]["command"]["acknowledged_at"] is not None

    after_ack = api.get("/api/v1/commands", headers=auth(destination_key))
    assert after_ack.json()["data"]["commands"] == []


@pytest.mark.parametrize(
    ("terminal_event_type", "terminal_status"),
    [
        ("SALE", "SOLD"),
        ("ISSUE", "ISSUED"),
    ],
)
def test_snapshot_cannot_resurrect_terminal_stock(
    api,
    terminal_event_type,
    terminal_status,
):
    api, db_session = api
    node, api_key = create_node_with_key(db_session, terminal_status)
    serial_number = f"QR-NO-RESURRECT-{terminal_status}"

    assert (
        post(
            api,
            api_key,
            event(1, "STOCK_SNAPSHOT", [item(serial_number)]),
        ).status_code
        == 200
    )
    terminal = item(serial_number, status=terminal_status)
    assert (
        post(
            api,
            api_key,
            event(2, terminal_event_type, [terminal], party_name="Counterparty"),
        ).status_code
        == 200
    )

    resurrection = item(serial_number, status="IN_STOCK")
    resurrection["warehouse"] = "UNTRUSTED-RESTOCK"
    response = post(
        api,
        api_key,
        event(3, "STOCK_SNAPSHOT", [resurrection]),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STOCK_STATUS_CONFLICT"
    serial = db_session.scalar(select(Serial).where(Serial.serial_number == serial_number))
    stock = db_session.scalar(select(NetworkStock).where(NetworkStock.serial_id == serial.id))
    db_session.refresh(node)
    assert serial.status == terminal_status
    assert stock.status == terminal_status
    assert serial.warehouse == "MAIN"
    assert node.last_sequence == 2


def test_snapshot_same_status_relocation_updates_only_metadata(api):
    api, db_session = api
    _node, api_key = create_node_with_key(db_session, "RELOCATE")
    serial_number = "QR-SNAPSHOT-RELOCATE"
    assert (
        post(
            api,
            api_key,
            event(1, "STOCK_SNAPSHOT", [item(serial_number)]),
        ).status_code
        == 200
    )

    relocated = item(serial_number)
    relocated["warehouse"] = "SECONDARY"
    response = post(
        api,
        api_key,
        event(2, "STOCK_SNAPSHOT", [relocated]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["acknowledgements"][0]["result"] == {
        "created": 0,
        "updated": 1,
    }
    serial = db_session.scalar(select(Serial).where(Serial.serial_number == serial_number))
    stock = db_session.scalar(select(NetworkStock).where(NetworkStock.serial_id == serial.id))
    assert serial.status == "IN_STOCK"
    assert stock.status == "IN_STOCK"
    assert serial.warehouse == "SECONDARY"
    assert stock.last_event.sequence == 2


def test_valid_replacement_retires_old_and_preserves_origin_product(api):
    api, db_session = api
    node, api_key = create_node_with_key(db_session, "REPLACE")
    old_number = "QR-REPLACE-OLD"
    new_number = "QR-REPLACE-NEW"
    assert (
        post(
            api,
            api_key,
            event(1, "STOCK_SNAPSHOT", [item(old_number)]),
        ).status_code
        == 200
    )

    old_item = item(old_number, status="INVALID")
    old_item["warehouse"] = "REPLACEMENT-SHELF"
    new_item = item(new_number, status="IN_STOCK")
    new_item["warehouse"] = "REPLACEMENT-SHELF"
    response = post(
        api,
        api_key,
        event(
            2,
            "STOCK_SNAPSHOT",
            [old_item, new_item],
            reason_code="QR_REPLACEMENT",
        ),
    )

    assert response.status_code == 200, response.text
    old_serial = db_session.scalar(select(Serial).where(Serial.serial_number == old_number))
    replacement = db_session.scalar(select(Serial).where(Serial.serial_number == new_number))
    old_stock = db_session.scalar(
        select(NetworkStock).where(NetworkStock.serial_id == old_serial.id)
    )
    new_stock = db_session.scalar(
        select(NetworkStock).where(NetworkStock.serial_id == replacement.id)
    )
    assert old_serial.status == "INVALID"
    assert old_serial.active is False
    assert old_serial.replaced_by_id == replacement.id
    assert replacement.status == "IN_STOCK"
    assert replacement.active is True
    assert replacement.product_id == old_serial.product_id
    assert replacement.warehouse == "REPLACEMENT-SHELF"
    assert old_stock.status == "INVALID"
    assert new_stock.status == "IN_STOCK"
    assert new_stock.current_franchise_id == node.id
    assert new_stock.origin_franchise_id == old_stock.origin_franchise_id

    history = db_session.scalars(
        select(InventoryTransaction)
        .where(
            InventoryTransaction.transaction_type == "QR_REPLACEMENT",
        )
        .order_by(InventoryTransaction.id)
    ).all()
    assert [(row.serial_number, row.status_from, row.status_to) for row in history] == [
        (old_number, "IN_STOCK", "INVALID"),
        (new_number, None, "IN_STOCK"),
    ]


@pytest.mark.parametrize(
    ("new_status", "new_product_code", "expected_code"),
    [
        ("GENERATED", "SKU-1", "INVALID_REPLACEMENT_TRANSITION"),
        ("IN_STOCK", "OTHER-SKU", "PRODUCT_IDENTITY_CONFLICT"),
    ],
)
def test_invalid_replacement_is_atomic(
    api,
    new_status,
    new_product_code,
    expected_code,
):
    api, db_session = api
    node, api_key = create_node_with_key(db_session, expected_code[-8:])
    old_number = f"QR-INVALID-OLD-{new_status}-{new_product_code}"
    new_number = f"QR-INVALID-NEW-{new_status}-{new_product_code}"
    assert (
        post(
            api,
            api_key,
            event(1, "STOCK_SNAPSHOT", [item(old_number)]),
        ).status_code
        == 200
    )

    response = post(
        api,
        api_key,
        event(
            2,
            "STOCK_SNAPSHOT",
            [
                item(old_number, status="INVALID"),
                item(
                    new_number,
                    status=new_status,
                    product_code=new_product_code,
                ),
            ],
            reason_code="QR_REPLACEMENT",
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code
    old_serial = db_session.scalar(select(Serial).where(Serial.serial_number == old_number))
    assert old_serial.status == "IN_STOCK"
    assert old_serial.active is True
    assert old_serial.replaced_by_id is None
    assert db_session.scalar(select(Serial).where(Serial.serial_number == new_number)) is None
    assert (
        db_session.scalar(
            select(InventoryTransaction).where(
                InventoryTransaction.transaction_type == "QR_REPLACEMENT"
            )
        )
        is None
    )
    db_session.refresh(node)
    assert node.last_sequence == 1
