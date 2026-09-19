"""Master reservations are the authority for QR replacement events."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import NetworkStock, NodeCommand, Serial, SerialStatus
from app.network_schemas import NetworkEventV1
from app.services.network_ingest import NetworkIngestError, ingest_events
from app.services.node_auth import create_franchise_node
from app.services.qr_allocation import allocate_qr_serials, get_or_create_franchise_product
from app.services.qr_replacement_intent import (
    QrReplacementError,
    QrReplacementIntent,
    request_qr_replacement,
)


def _created_stock(db):
    node = create_franchise_node(db, code="F-QR", name="QR FRANCHISE", location="CITY")
    product = get_or_create_franchise_product(
        db,
        node,
        product_code="SKU1",
        product_name="Test product",
        tally_stock_item_name="Test product",
        hsn="1234",
        gst_rate=18,
    )
    old_code = allocate_qr_serials(
        db,
        node=node,
        product=product,
        product_code="SKU1",
        quantity=1,
    )[0]
    db.commit()
    return node, old_code


def _replacement_event(old_code, new_code, command_id, *, sequence=1):
    item = {
        "product_code": "SKU1",
        "product_name": "Test product",
        "tally_stock_item_name": "Test product",
        "hsn": "1234",
        "gst_rate": 18,
        "unit": "Pcs",
        "rate": 0,
    }
    return NetworkEventV1.model_validate(
        {
            "event_id": str(uuid4()),
            "sequence": sequence,
            "schema_version": 1,
            "type": "STOCK_SNAPSHOT",
            "occurred_at": datetime.now(UTC).isoformat(),
            "reference": command_id,
            "reason_code": "QR_REPLACEMENT",
            "items": [
                {**item, "serial_number": old_code, "status": "INVALID"},
                {**item, "serial_number": new_code, "status": "GENERATED"},
            ],
        }
    )


def test_replacement_reserves_new_identity_without_creating_stock(db_session):
    node, old_code = _created_stock(db_session)
    intent, command = request_qr_replacement(
        db_session,
        franchise=node,
        old_serial_number=old_code.lower(),
        requested_by="admin",
    )
    db_session.commit()

    assert command.command_type == "QR_REPLACE"
    assert intent.new_serial_number.startswith("SQR-")
    assert db_session.scalar(
        select(Serial).where(Serial.serial_number == intent.new_serial_number)
    ) is None
    assert db_session.scalar(
        select(QrReplacementIntent).where(QrReplacementIntent.old_serial_id == intent.old_serial_id)
    ) is not None
    with pytest.raises(QrReplacementError, match="already been requested"):
        request_qr_replacement(
            db_session,
            franchise=node,
            old_serial_number=old_code,
            requested_by="admin",
        )


def test_replacement_event_requires_exact_command_and_completes_reservation(db_session):
    node, old_code = _created_stock(db_session)
    intent, command = request_qr_replacement(
        db_session,
        franchise=node,
        old_serial_number=old_code,
        requested_by="admin",
    )
    new_code = intent.new_serial_number
    command_id = command.public_id
    db_session.commit()

    with pytest.raises(NetworkIngestError) as mismatch:
        ingest_events(
            db_session,
            node,
            [_replacement_event(old_code, new_code, str(uuid4()))],
        )
    assert mismatch.value.code == "QR_REPLACEMENT_NOT_RESERVED"
    db_session.rollback()
    assert db_session.scalar(select(Serial).where(Serial.serial_number == new_code)) is None

    ingest_events(db_session, node, [_replacement_event(old_code, new_code, command_id)])
    db_session.commit()
    old = db_session.scalar(select(Serial).where(Serial.serial_number == old_code))
    new = db_session.scalar(select(Serial).where(Serial.serial_number == new_code))
    assert old.status == SerialStatus.INVALID.value
    assert not old.active
    assert old.replaced_by_id == new.id
    assert new.status == SerialStatus.GENERATED.value
    assert db_session.scalar(select(NetworkStock).where(NetworkStock.serial_id == new.id)).current_franchise_id == node.id
    assert db_session.get(QrReplacementIntent, intent.id).completed_at is not None
    assert db_session.get(NodeCommand, command.id).command_type == "QR_REPLACE"
