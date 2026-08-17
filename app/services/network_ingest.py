from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Batch,
    BatchItem,
    BatchStatus,
    BatchType,
    FranchiseNode,
    InboundEvent,
    InventoryTransaction,
    NetworkStock,
    NodeCommand,
    Product,
    Receipt,
    ReceiptStatus,
    Serial,
    SerialStatus,
    StockTransfer,
    StockTransferItem,
    TransactionType,
    TransferStatus,
    utc_now,
)
from app.network_schemas import (
    EventBatchRequest,
    NetworkEventItem,
    NetworkEventType,
    NetworkEventV1,
)
from app.services.node_auth import ensure_node_service_user

TALLY_EVENT_TYPES = {
    NetworkEventType.PURCHASE,
    NetworkEventType.RECEIVE,
    NetworkEventType.SALE,
    NetworkEventType.SALES_RETURN,
}
INVENTORY_EVENT_TYPES = {
    NetworkEventType.PURCHASE,
    NetworkEventType.RECEIVE,
    NetworkEventType.SALE,
    NetworkEventType.SALES_RETURN,
    NetworkEventType.PURCHASE_RETURN,
    NetworkEventType.ISSUE,
    NetworkEventType.AUDIT,
}
AVAILABLE_STOCK_STATUSES = {
    SerialStatus.IN_STOCK.value,
    SerialStatus.RETURNED.value,
}
SNAPSHOT_ENROLLMENT_STATUSES = {
    SerialStatus.GENERATED.value,
    SerialStatus.IN_STOCK.value,
}
REPLACEABLE_SNAPSHOT_STATUSES = {
    SerialStatus.GENERATED.value,
    SerialStatus.IN_STOCK.value,
    SerialStatus.RETURNED.value,
    SerialStatus.DAMAGED.value,
}


class NetworkIngestError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _json_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _event_payload(event: NetworkEventV1) -> tuple[str, str]:
    payload_json = _canonical_json(event.model_dump(mode="json"))
    return payload_json, _json_hash(payload_json)


def _command_payload(value: dict[str, Any]) -> tuple[str, str]:
    payload_json = _canonical_json(value)
    return payload_json, _json_hash(payload_json)


def _namespaced_code(node: FranchiseNode, product_code: str) -> str:
    value = f"{node.code}:{product_code}"
    if len(value) <= 80:
        return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()
    return f"{value[:67]}:{suffix}"


def _event_id(event: NetworkEventV1) -> str:
    return str(event.event_id)


def _transfer_id(event: NetworkEventV1) -> str:
    return str(event.transfer_id or event.event_id)


def _batch_number(node: FranchiseNode, event: NetworkEventV1) -> str:
    # Globally unique while remaining easy to identify in reports and Tally.
    return f"NET-{node.code[:36]}-{event.sequence:012d}"


def _serialize_command(command: NodeCommand) -> dict[str, Any]:
    return {
        "command_id": command.public_id,
        "type": command.command_type,
        "payload": json.loads(command.payload_json),
        "created_at": command.created_at.isoformat(),
        "acknowledged_at": (
            command.acknowledged_at.isoformat() if command.acknowledged_at else None
        ),
    }


def queue_node_command(
    db: Session,
    *,
    target: FranchiseNode,
    command_type: str,
    payload: dict[str, Any],
) -> NodeCommand:
    payload_json, payload_hash = _command_payload(payload)
    command = NodeCommand(
        target_franchise_id=target.id,
        command_type=command_type,
        payload_json=payload_json,
        payload_hash=payload_hash,
    )
    db.add(command)
    db.flush()
    return command


def list_unacknowledged_commands(
    db: Session,
    node: FranchiseNode,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    commands = db.scalars(
        select(NodeCommand)
        .where(
            NodeCommand.target_franchise_id == node.id,
            NodeCommand.acknowledged_at.is_(None),
        )
        .order_by(NodeCommand.id)
        .limit(max(1, min(limit, 100)))
    ).all()
    return [_serialize_command(command) for command in commands]


def acknowledge_command(
    db: Session,
    node: FranchiseNode,
    command_public_id: str,
) -> dict[str, Any]:
    command = db.scalar(select(NodeCommand).where(NodeCommand.public_id == command_public_id))
    if command is None:
        raise NetworkIngestError(404, "COMMAND_NOT_FOUND", "The command was not found.")
    if command.target_franchise_id != node.id:
        raise NetworkIngestError(
            403,
            "COMMAND_FORBIDDEN",
            "This command belongs to a different franchise.",
        )
    if command.acknowledged_at is None:
        command.acknowledged_at = utc_now()
        db.flush()
    return _serialize_command(command)


def _find_serial_and_stock(
    db: Session,
    serial_number: str,
) -> tuple[Serial | None, NetworkStock | None]:
    serial = db.scalar(select(Serial).where(Serial.serial_number == serial_number))
    if serial is None:
        return None, None
    stock = db.scalar(select(NetworkStock).where(NetworkStock.serial_id == serial.id))
    return serial, stock


def _ensure_product(db: Session, node: FranchiseNode, item: NetworkEventItem) -> Product:
    product_code = _namespaced_code(node, item.product_code)
    product = db.scalar(select(Product).where(Product.product_code == product_code))
    if product is None:
        product = Product(
            product_code=product_code,
            product_name=item.product_name,
            hsn=item.hsn,
            gst_rate=item.gst_rate,
            unit=item.unit,
            default_rate=item.rate,
            tally_stock_item_name=item.tally_stock_item_name,
            purchase_qr_print_allowed=False,
            active=True,
        )
        db.add(product)
        db.flush()
    return product


def _create_serial(
    db: Session,
    node: FranchiseNode,
    item: NetworkEventItem,
    *,
    initial_status: str = SerialStatus.GENERATED.value,
) -> Serial:
    product = _ensure_product(db, node, item)
    serial = Serial(
        serial_number=item.serial_number,
        product_id=product.id,
        status=initial_status,
        product_batch_number=item.product_batch_number,
        mfg_date=item.mfg_date,
        expiry_date=item.expiry_date,
        warehouse=item.warehouse or node.tally_godown_name,
        active=True,
    )
    db.add(serial)
    db.flush()
    return serial


def _update_serial_snapshot(
    serial: Serial,
    node: FranchiseNode,
    item: NetworkEventItem,
) -> None:
    if item.product_batch_number is not None:
        serial.product_batch_number = item.product_batch_number
    if item.mfg_date is not None:
        serial.mfg_date = item.mfg_date
    if item.expiry_date is not None:
        serial.expiry_date = item.expiry_date
    if item.warehouse is not None or node.tally_godown_name:
        serial.warehouse = item.warehouse or node.tally_godown_name


def _require_owned_stock(
    db: Session,
    node: FranchiseNode,
    serial_number: str,
) -> tuple[Serial, NetworkStock]:
    serial, stock = _find_serial_and_stock(db, serial_number)
    if serial is None or stock is None:
        raise NetworkIngestError(
            409,
            "STOCK_UNKNOWN",
            f"Serial {serial_number} is not known to the network.",
        )
    if stock.current_franchise_id != node.id:
        raise NetworkIngestError(
            409,
            "STOCK_OWNERSHIP_CONFLICT",
            f"Serial {serial_number} is owned by another franchise.",
        )
    return serial, stock


def _validate_available(serial: Serial, stock: NetworkStock) -> None:
    if (
        not serial.active
        or serial.status not in AVAILABLE_STOCK_STATUSES
        or stock.status not in AVAILABLE_STOCK_STATUSES
    ):
        raise NetworkIngestError(
            409,
            "STOCK_NOT_AVAILABLE",
            f"Serial {serial.serial_number} is not available from status {stock.status}.",
        )


def _identity_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _validate_snapshot_product_identity(
    db: Session,
    *,
    serial: Serial,
    origin: FranchiseNode,
    item: NetworkEventItem,
) -> None:
    product = db.get(Product, serial.product_id)
    if product is None:
        raise NetworkIngestError(
            409,
            "STOCK_DATA_CONFLICT",
            f"Serial {serial.serial_number} has no product record.",
        )

    expected_code = _namespaced_code(origin, item.product_code)
    mismatches: list[str] = []
    if product.product_code != expected_code:
        mismatches.append("product_code")
    comparisons = {
        "product_name": (product.product_name, item.product_name),
        "tally_stock_item_name": (
            product.tally_stock_item_name,
            item.tally_stock_item_name,
        ),
        "hsn": (product.hsn, item.hsn),
        "unit": (product.unit, item.unit),
    }
    mismatches.extend(
        field
        for field, (current, incoming) in comparisons.items()
        if _identity_text(current) != _identity_text(incoming)
    )
    if abs(float(product.gst_rate or 0) - float(item.gst_rate)) > 0.0001:
        mismatches.append("gst_rate")
    if mismatches:
        raise NetworkIngestError(
            409,
            "PRODUCT_IDENTITY_CONFLICT",
            (
                f"Serial {serial.serial_number} does not match its origin product "
                f"({', '.join(mismatches)})."
            ),
        )


def _snapshot_origin(db: Session, stock: NetworkStock) -> FranchiseNode:
    origin = db.get(FranchiseNode, stock.origin_franchise_id)
    if origin is None:
        raise NetworkIngestError(
            409,
            "STOCK_DATA_CONFLICT",
            f"Stock record {stock.id} has no origin franchise.",
        )
    return origin


def _replacement_snapshot_event(
    db: Session,
    node: FranchiseNode,
    inbound: InboundEvent,
    event: NetworkEventV1,
) -> dict[str, Any]:
    if len(event.items) != 2:
        raise NetworkIngestError(
            422,
            "INVALID_REPLACEMENT_SHAPE",
            "QR_REPLACEMENT requires exactly one old serial and one new serial.",
        )

    resolved = [(item, *_find_serial_and_stock(db, item.serial_number)) for item in event.items]
    old_candidates = [
        (item, serial, stock)
        for item, serial, stock in resolved
        if serial is not None and stock is not None
    ]
    new_candidates = [item for item, serial, stock in resolved if serial is None and stock is None]
    if len(old_candidates) != 1 or len(new_candidates) != 1:
        raise NetworkIngestError(
            409,
            "INVALID_REPLACEMENT_SHAPE",
            (
                "QR_REPLACEMENT requires exactly one tracked old serial and "
                "one globally unknown new serial."
            ),
        )

    old_item, old_serial, old_stock = old_candidates[0]
    new_item = new_candidates[0]
    if old_stock.current_franchise_id != node.id:
        raise NetworkIngestError(
            409,
            "STOCK_OWNERSHIP_CONFLICT",
            f"Serial {old_serial.serial_number} is owned by another franchise.",
        )
    if not old_serial.active or old_stock.status not in REPLACEABLE_SNAPSHOT_STATUSES:
        raise NetworkIngestError(
            409,
            "INVALID_REPLACEMENT_TRANSITION",
            (
                f"Serial {old_serial.serial_number} cannot be replaced from "
                f"status {old_stock.status}."
            ),
        )
    if old_item.status != SerialStatus.INVALID.value:
        raise NetworkIngestError(
            409,
            "INVALID_REPLACEMENT_TRANSITION",
            "The old replacement serial must transition to INVALID.",
        )

    target_status = (
        SerialStatus.GENERATED.value
        if old_stock.status == SerialStatus.GENERATED.value
        else SerialStatus.IN_STOCK.value
    )
    if new_item.status != target_status:
        raise NetworkIngestError(
            409,
            "INVALID_REPLACEMENT_TRANSITION",
            (f"The replacement for {old_stock.status} stock must start as {target_status}."),
        )

    origin = _snapshot_origin(db, old_stock)
    _validate_snapshot_product_identity(
        db,
        serial=old_serial,
        origin=origin,
        item=old_item,
    )
    _validate_snapshot_product_identity(
        db,
        serial=old_serial,
        origin=origin,
        item=new_item,
    )

    previous_status = old_stock.status
    _update_serial_snapshot(old_serial, node, old_item)
    replacement = Serial(
        serial_number=new_item.serial_number,
        product_id=old_serial.product_id,
        status=target_status,
        active=True,
        product_batch_number=old_serial.product_batch_number,
        mfg_date=old_serial.mfg_date,
        expiry_date=old_serial.expiry_date,
        warehouse=old_serial.warehouse,
        warehouse_level=old_serial.warehouse_level,
        location_id=old_serial.location_id,
    )
    db.add(replacement)
    db.flush()

    old_serial.status = SerialStatus.INVALID.value
    old_serial.active = False
    old_serial.replaced_by_id = replacement.id
    old_stock.status = SerialStatus.INVALID.value
    old_stock.last_event_id = inbound.id
    db.add(
        NetworkStock(
            serial_id=replacement.id,
            current_franchise_id=node.id,
            origin_franchise_id=old_stock.origin_franchise_id,
            status=target_status,
            last_event_id=inbound.id,
        )
    )

    service_user = ensure_node_service_user(db, node)
    reference_number = (event.reference or _event_id(event))[:80]
    db.add_all(
        [
            InventoryTransaction(
                transaction_type=TransactionType.QR_REPLACEMENT.value,
                serial_id=old_serial.id,
                product_id=old_serial.product_id,
                user_id=service_user.id,
                serial_number=old_serial.serial_number,
                status_from=previous_status,
                status_to=SerialStatus.INVALID.value,
                reason_code="QR_REPLACEMENT",
                tally_reference=event.reference,
                reference_number=reference_number,
                notes=(
                    f"Franchise {node.code}; replaced by "
                    f"{replacement.serial_number}; actor {event.actor or 'unknown'}"
                ),
                created_at=event.occurred_at,
            ),
            InventoryTransaction(
                transaction_type=TransactionType.QR_REPLACEMENT.value,
                serial_id=replacement.id,
                product_id=replacement.product_id,
                user_id=service_user.id,
                serial_number=replacement.serial_number,
                status_from=None,
                status_to=target_status,
                reason_code="QR_REPLACEMENT",
                tally_reference=event.reference,
                reference_number=reference_number,
                notes=(
                    f"Franchise {node.code}; replaces "
                    f"{old_serial.serial_number}; actor {event.actor or 'unknown'}"
                ),
                created_at=event.occurred_at,
            ),
        ]
    )
    return {
        "created": 1,
        "updated": 1,
        "replacement": {
            "old_serial_number": old_serial.serial_number,
            "new_serial_number": replacement.serial_number,
            "status": target_status,
        },
    }


def _snapshot_event(
    db: Session,
    node: FranchiseNode,
    inbound: InboundEvent,
    event: NetworkEventV1,
) -> dict[str, Any]:
    valid_statuses = {status.value for status in SerialStatus}
    if (event.reason_code or "").strip().upper() == "QR_REPLACEMENT":
        return _replacement_snapshot_event(db, node, inbound, event)

    created = 0
    updated = 0
    for item in event.items:
        if item.status not in valid_statuses:
            raise NetworkIngestError(
                422,
                "INVALID_STOCK_STATUS",
                f"{item.status} is not a supported stock status.",
            )
        serial, stock = _find_serial_and_stock(db, item.serial_number)
        if serial is None:
            if item.status not in SNAPSHOT_ENROLLMENT_STATUSES:
                raise NetworkIngestError(
                    409,
                    "INVALID_SNAPSHOT_ENROLLMENT_STATUS",
                    (
                        f"New serial {item.serial_number} may only be enrolled "
                        "as GENERATED or IN_STOCK."
                    ),
                )
            serial = _create_serial(
                db,
                node,
                item,
                initial_status=item.status,
            )
            _validate_snapshot_product_identity(
                db,
                serial=serial,
                origin=node,
                item=item,
            )
            stock = NetworkStock(
                serial_id=serial.id,
                current_franchise_id=node.id,
                origin_franchise_id=node.id,
                status=item.status,
                last_event_id=inbound.id,
            )
            db.add(stock)
            created += 1
            continue

        if stock is None:
            raise NetworkIngestError(
                409,
                "STOCK_TRACKING_CONFLICT",
                (
                    f"Serial {item.serial_number} already exists but has no "
                    "network ownership record."
                ),
            )
        if stock.current_franchise_id != node.id:
            raise NetworkIngestError(
                409,
                "STOCK_OWNERSHIP_CONFLICT",
                f"Serial {item.serial_number} is already owned by another franchise.",
            )
        if item.status != stock.status:
            raise NetworkIngestError(
                409,
                "STOCK_STATUS_CONFLICT",
                (
                    f"Snapshot status {item.status} cannot replace authoritative "
                    f"status {stock.status} for serial {item.serial_number}."
                ),
            )
        _validate_snapshot_product_identity(
            db,
            serial=serial,
            origin=_snapshot_origin(db, stock),
            item=item,
        )
        # NetworkStock is authoritative if a Master-side mirror was edited
        # outside this ingestion path. The node is only authorizing metadata.
        serial.status = stock.status
        stock.last_event_id = inbound.id
        _update_serial_snapshot(serial, node, item)
        updated += 1
    return {"created": created, "updated": updated}


def _transition_for_event(
    event: NetworkEventV1,
    current_status: str,
) -> tuple[str, str]:
    if event.type in {NetworkEventType.PURCHASE, NetworkEventType.RECEIVE}:
        if current_status not in {
            SerialStatus.GENERATED.value,
            SerialStatus.PURCHASE_RETURN.value,
        }:
            raise NetworkIngestError(
                409,
                "INVALID_STOCK_TRANSITION",
                f"Purchase/receive cannot start from {current_status}.",
            )
        return SerialStatus.IN_STOCK.value, TransactionType.PURCHASE.value
    if event.type == NetworkEventType.SALE:
        if current_status not in AVAILABLE_STOCK_STATUSES:
            raise NetworkIngestError(
                409,
                "INVALID_STOCK_TRANSITION",
                f"Sale cannot start from {current_status}.",
            )
        return SerialStatus.SOLD.value, TransactionType.SALE.value
    if event.type == NetworkEventType.SALES_RETURN:
        if current_status != SerialStatus.SOLD.value:
            raise NetworkIngestError(
                409,
                "INVALID_STOCK_TRANSITION",
                f"Sales return cannot start from {current_status}.",
            )
        damaged = (event.reason_code or "").upper() in {"DAMAGED", "EXPIRED"}
        return (
            SerialStatus.DAMAGED.value if damaged else SerialStatus.IN_STOCK.value,
            TransactionType.SALES_RETURN.value,
        )
    if event.type == NetworkEventType.PURCHASE_RETURN:
        if current_status not in AVAILABLE_STOCK_STATUSES:
            raise NetworkIngestError(
                409,
                "INVALID_STOCK_TRANSITION",
                f"Purchase return cannot start from {current_status}.",
            )
        return SerialStatus.PURCHASE_RETURN.value, TransactionType.PURCHASE_RETURN.value
    if event.type == NetworkEventType.ISSUE:
        if current_status != SerialStatus.IN_STOCK.value:
            raise NetworkIngestError(
                409,
                "INVALID_STOCK_TRANSITION",
                f"Issue cannot start from {current_status}.",
            )
        return SerialStatus.ISSUED.value, TransactionType.ISSUE.value
    if event.type == NetworkEventType.AUDIT:
        return current_status, TransactionType.AUDIT.value
    raise NetworkIngestError(422, "UNSUPPORTED_EVENT", "Unsupported inventory event.")


def _batch_type(event_type: NetworkEventType) -> str:
    return {
        NetworkEventType.PURCHASE: BatchType.PURCHASE.value,
        NetworkEventType.RECEIVE: BatchType.RECEIVE.value,
        NetworkEventType.SALE: BatchType.SALE.value,
        NetworkEventType.SALES_RETURN: BatchType.SALES_RETURN.value,
        NetworkEventType.PURCHASE_RETURN: BatchType.PURCHASE_RETURN.value,
        NetworkEventType.ISSUE: BatchType.ISSUE.value,
        NetworkEventType.AUDIT: BatchType.AUDIT.value,
    }[event_type]


def _inventory_event(
    db: Session,
    node: FranchiseNode,
    inbound: InboundEvent,
    event: NetworkEventV1,
) -> dict[str, Any]:
    service_user = ensure_node_service_user(db, node)
    batch = Batch(
        batch_number=_batch_number(node, event),
        batch_type=_batch_type(event.type),
        party_name=event.party_name,
        party_state=event.party_state,
        party_gst_registration_type=event.party_gst_registration_type,
        party_gst_name=event.party_gst_name,
        party_gstin=event.party_gstin,
        gst_treatment=event.gst_treatment,
        gst_cgst_rate=event.gst_cgst_rate,
        gst_sgst_rate=event.gst_sgst_rate,
        gst_igst_rate=event.gst_igst_rate,
        reason_code=event.reason_code,
        user_id=service_user.id,
        status=(
            BatchStatus.PENDING_SYNC.value
            if event.type in TALLY_EVENT_TYPES
            else BatchStatus.CLOSED.value
        ),
        tally_reference=event.reference,
        notes=f"Mirrored from franchise {node.code}; inbound event {_event_id(event)}",
        sync_remote_id=_event_id(event),
        submitted_at=event.occurred_at,
    )
    db.add(batch)
    db.flush()
    inbound.tally_batch_id = batch.id

    for item in event.items:
        serial, stock = _find_serial_and_stock(db, item.serial_number)
        if serial is None:
            if event.type not in {NetworkEventType.PURCHASE, NetworkEventType.RECEIVE}:
                raise NetworkIngestError(
                    409,
                    "STOCK_UNKNOWN",
                    f"Serial {item.serial_number} is not known to the network.",
                )
            serial = _create_serial(db, node, item)
            stock = None

        if stock is not None and stock.current_franchise_id != node.id:
            raise NetworkIngestError(
                409,
                "STOCK_OWNERSHIP_CONFLICT",
                f"Serial {item.serial_number} is owned by another franchise.",
            )
        if stock is None:
            if event.type not in {NetworkEventType.PURCHASE, NetworkEventType.RECEIVE}:
                raise NetworkIngestError(
                    409,
                    "STOCK_UNKNOWN",
                    f"Serial {item.serial_number} has no network ownership record.",
                )
            stock = NetworkStock(
                serial_id=serial.id,
                current_franchise_id=node.id,
                origin_franchise_id=node.id,
                status=serial.status,
                last_event_id=inbound.id,
            )
            db.add(stock)

        previous_status = stock.status
        target_status, transaction_type = _transition_for_event(event, previous_status)
        if serial.status != previous_status:
            # NetworkStock is authoritative. A mismatch indicates local master
            # data was changed outside the network ingestion path.
            serial.status = previous_status
        serial.status = target_status
        stock.status = target_status
        stock.last_event_id = inbound.id
        _update_serial_snapshot(serial, node, item)

        db.add(
            BatchItem(
                batch_id=batch.id,
                serial_id=serial.id,
                quantity=1,
                rate=item.rate,
            )
        )
        db.add(
            InventoryTransaction(
                transaction_type=transaction_type,
                serial_id=serial.id,
                product_id=serial.product_id,
                batch_id=batch.id,
                user_id=service_user.id,
                serial_number=serial.serial_number,
                status_from=previous_status,
                status_to=target_status,
                reason_code=event.reason_code,
                tally_reference=event.reference,
                reference_number=batch.batch_number,
                notes=f"Franchise {node.code}; actor {event.actor or 'unknown'}",
                created_at=event.occurred_at,
            )
        )
    return {
        "batch_id": batch.id,
        "batch_number": batch.batch_number,
        "batch_status": batch.status,
        "item_count": len(event.items),
    }


def _dispatch_transfer(
    db: Session,
    node: FranchiseNode,
    inbound: InboundEvent,
    event: NetworkEventV1,
) -> dict[str, Any]:
    destination = db.scalar(
        select(FranchiseNode).where(
            FranchiseNode.code == event.destination_franchise_code,
            FranchiseNode.active.is_(True),
        )
    )
    if destination is None:
        raise NetworkIngestError(
            422,
            "DESTINATION_NOT_FOUND",
            "The destination franchise does not exist or is inactive.",
        )
    if destination.id == node.id:
        raise NetworkIngestError(
            409,
            "INVALID_TRANSFER",
            "A franchise cannot transfer stock to itself.",
        )

    existing_reference = db.scalar(
        select(StockTransfer.id).where(
            StockTransfer.source_franchise_id == node.id,
            StockTransfer.reference == event.reference,
        )
    )
    if existing_reference is not None:
        raise NetworkIngestError(
            409,
            "TRANSFER_REFERENCE_CONFLICT",
            "That transfer reference has already been used.",
        )
    public_id = _transfer_id(event)
    if db.scalar(select(StockTransfer.id).where(StockTransfer.public_id == public_id)) is not None:
        raise NetworkIngestError(
            409,
            "TRANSFER_ID_CONFLICT",
            "That transfer ID has already been used.",
        )

    validated: list[tuple[NetworkEventItem, Serial, NetworkStock]] = []
    for item in event.items:
        serial, stock = _require_owned_stock(db, node, item.serial_number)
        _validate_available(serial, stock)
        validated.append((item, serial, stock))

    transfer = StockTransfer(
        public_id=public_id,
        source_franchise_id=node.id,
        destination_franchise_id=destination.id,
        reference=event.reference or public_id,
        status=TransferStatus.DISPATCHED.value,
        dispatch_event_id=inbound.id,
    )
    db.add(transfer)
    db.flush()

    for _item, serial, stock in validated:
        serial.status = "IN_TRANSIT"
        stock.status = "IN_TRANSIT"
        stock.last_event_id = inbound.id
        db.add(
            StockTransferItem(
                transfer_id=transfer.id,
                serial_id=serial.id,
                quantity=1,
                received_quantity=0,
            )
        )

    command = queue_node_command(
        db,
        target=destination,
        command_type="TRANSFER_AVAILABLE",
        payload={
            "transfer": {
                "transfer_uuid": transfer.public_id,
                "source_franchise_code": node.code,
                "destination_franchise_code": destination.code,
                "status": transfer.status,
                "dispatched_at": event.occurred_at.isoformat(),
                "notes": event.reference,
            },
            "items": [
                {
                    "manifest_serial_number": item.serial_number,
                    "product": {
                        "product_code": item.product_code,
                        "product_name": item.product_name,
                        "hsn": item.hsn,
                        "gst_rate": item.gst_rate,
                        "unit": item.unit,
                        "default_rate": item.rate,
                        "tally_stock_item_name": item.tally_stock_item_name,
                    },
                    "serial": {
                        "serial_number": item.serial_number,
                        "status": item.status,
                        "product_batch_number": item.product_batch_number,
                        "mfg_date": item.mfg_date.isoformat() if item.mfg_date else None,
                        "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None,
                        "warehouse": item.warehouse,
                    },
                }
                for item, _serial, _stock in validated
            ],
        },
    )
    return {
        "transfer_id": transfer.public_id,
        "transfer_status": transfer.status,
        "destination_franchise_code": destination.code,
        "command_id": command.public_id,
        "item_count": len(validated),
    }


def _receive_transfer(
    db: Session,
    node: FranchiseNode,
    inbound: InboundEvent,
    event: NetworkEventV1,
) -> dict[str, Any]:
    transfer = db.scalar(
        select(StockTransfer).where(StockTransfer.public_id == str(event.transfer_id))
    )
    if transfer is None:
        raise NetworkIngestError(404, "TRANSFER_NOT_FOUND", "The transfer was not found.")
    if transfer.destination_franchise_id != node.id:
        raise NetworkIngestError(
            403,
            "TRANSFER_FORBIDDEN",
            "Only the destination franchise can receive this transfer.",
        )
    if transfer.status == TransferStatus.RECEIVED.value:
        raise NetworkIngestError(
            409,
            "TRANSFER_ALREADY_RECEIVED",
            "This transfer has already been received in full.",
        )

    transfer_items = {item.serial.serial_number: item for item in transfer.items}
    received_serials: list[str] = []
    for event_item in event.items:
        transfer_item = transfer_items.get(event_item.serial_number)
        if transfer_item is None:
            raise NetworkIngestError(
                409,
                "TRANSFER_ITEM_CONFLICT",
                f"Serial {event_item.serial_number} is not part of this transfer.",
            )
        if transfer_item.received_quantity >= transfer_item.quantity:
            raise NetworkIngestError(
                409,
                "TRANSFER_ITEM_ALREADY_RECEIVED",
                f"Serial {event_item.serial_number} was already received.",
            )
        serial = transfer_item.serial
        stock = db.scalar(select(NetworkStock).where(NetworkStock.serial_id == serial.id))
        if (
            stock is None
            or stock.current_franchise_id != transfer.source_franchise_id
            or stock.status != "IN_TRANSIT"
        ):
            raise NetworkIngestError(
                409,
                "TRANSFER_STOCK_CONFLICT",
                f"Serial {event_item.serial_number} is no longer in transit.",
            )

        received_status = (
            event_item.status
            if event_item.status in AVAILABLE_STOCK_STATUSES
            else SerialStatus.IN_STOCK.value
        )
        transfer_item.received_quantity = transfer_item.quantity
        transfer_item.received_at = event.occurred_at
        transfer_item.last_receipt_event_id = inbound.id
        stock.current_franchise_id = node.id
        stock.status = received_status
        stock.last_event_id = inbound.id
        serial.status = received_status
        _update_serial_snapshot(serial, node, event_item)
        received_serials.append(serial.serial_number)

    all_received = all(item.received_quantity >= item.quantity for item in transfer.items)
    transfer.status = (
        TransferStatus.RECEIVED.value if all_received else TransferStatus.PARTIALLY_RECEIVED.value
    )
    transfer.updated_at = utc_now()
    if all_received:
        transfer.received_at = event.occurred_at

    source = transfer.source_franchise
    command = queue_node_command(
        db,
        target=source,
        command_type="TRANSFER_RECEIPT",
        payload={
            "transfer": {
                "transfer_uuid": transfer.public_id,
                "source_franchise_code": source.code,
                "destination_franchise_code": node.code,
                "status": transfer.status,
                "complete": all_received,
                "receipt_at": event.occurred_at.isoformat(),
            },
            "received_serial_numbers": received_serials,
        },
    )
    return {
        "transfer_id": transfer.public_id,
        "transfer_status": transfer.status,
        "received_item_count": len(received_serials),
        "command_id": command.public_id,
    }


def _apply_event(
    db: Session,
    node: FranchiseNode,
    inbound: InboundEvent,
    event: NetworkEventV1,
) -> dict[str, Any]:
    if event.type == NetworkEventType.HEARTBEAT:
        return {"heartbeat": True}
    if event.type == NetworkEventType.STOCK_SNAPSHOT:
        return _snapshot_event(db, node, inbound, event)
    if event.type in INVENTORY_EVENT_TYPES:
        return _inventory_event(db, node, inbound, event)
    if event.type == NetworkEventType.TRANSFER_DISPATCHED:
        return _dispatch_transfer(db, node, inbound, event)
    if event.type == NetworkEventType.TRANSFER_RECEIVED:
        return _receive_transfer(db, node, inbound, event)
    if event.type == NetworkEventType.RECEIPT_SUBMITTED:
        receipt = Receipt(
            lite_receipt_id=str(event.receipt_id),
            franchise_id=node.id,
            source_event_id=inbound.id,
            receipt_date=event.receipt_date,
            proof_image=base64.b64decode(event.proof_image_base64 or "", validate=True),
            proof_content_type=event.proof_content_type or "application/octet-stream",
            utr_number=event.utr_number or None,
            submitted_by=event.actor,
            status=ReceiptStatus.PENDING.value,
            created_at=event.occurred_at,
        )
        db.add(receipt)
        db.flush()
        return {"receipt_id": receipt.public_id, "status": receipt.status}
    raise NetworkIngestError(422, "UNSUPPORTED_EVENT", "The event type is not supported.")


def _load_locked_node(db: Session, node: FranchiseNode) -> FranchiseNode:
    locked = db.scalar(
        select(FranchiseNode)
        .where(FranchiseNode.id == node.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None or not locked.active:
        raise NetworkIngestError(403, "NODE_INACTIVE", "This franchise node is inactive.")
    return locked


def ingest_events(
    db: Session,
    node: FranchiseNode,
    request: EventBatchRequest | Iterable[NetworkEventV1],
) -> list[dict[str, Any]]:
    events = request.events if isinstance(request, EventBatchRequest) else list(request)
    locked_node = _load_locked_node(db, node)
    acknowledgements: list[dict[str, Any]] = []

    for event in events:
        payload_json, payload_hash = _event_payload(event)
        event_id = _event_id(event)

        existing_id = db.scalar(select(InboundEvent).where(InboundEvent.event_id == event_id))
        if existing_id is not None:
            if existing_id.franchise_id != locked_node.id:
                raise NetworkIngestError(
                    403,
                    "EVENT_FORBIDDEN",
                    "That event ID belongs to a different franchise.",
                )
            if existing_id.payload_hash != payload_hash:
                raise NetworkIngestError(
                    409,
                    "EVENT_ID_CONFLICT",
                    "The event ID was already used with a different body.",
                )
            acknowledgements.append(json.loads(existing_id.result_json))
            continue

        existing_sequence = db.scalar(
            select(InboundEvent).where(
                InboundEvent.franchise_id == locked_node.id,
                InboundEvent.sequence == event.sequence,
            )
        )
        if existing_sequence is not None:
            raise NetworkIngestError(
                409,
                "SEQUENCE_CONFLICT",
                "That sequence was already used by a different event.",
            )

        expected_sequence = locked_node.last_sequence + 1
        if event.sequence != expected_sequence:
            code = "SEQUENCE_GAP" if event.sequence > expected_sequence else "SEQUENCE_STALE"
            raise NetworkIngestError(
                409,
                code,
                f"Expected sequence {expected_sequence}, received {event.sequence}.",
                details={"expected_sequence": expected_sequence},
            )

        received_at = utc_now()
        inbound = InboundEvent(
            event_id=event_id,
            franchise_id=locked_node.id,
            sequence=event.sequence,
            schema_version=event.schema_version,
            event_type=event.type.value,
            occurred_at=event.occurred_at,
            received_at=received_at,
            reference=event.reference,
            actor=event.actor,
            payload_json=payload_json,
            payload_hash=payload_hash,
            result_json="{}",
        )
        db.add(inbound)
        db.flush()
        result = _apply_event(db, locked_node, inbound, event)
        acknowledgement = {
            "event_id": event_id,
            "sequence": event.sequence,
            "type": event.type.value,
            "status": "ACCEPTED",
            "received_at": received_at.isoformat(),
            "result": result,
        }
        inbound.result_json = _canonical_json(acknowledgement)
        locked_node.last_sequence = event.sequence
        locked_node.last_seen_at = received_at
        db.flush()
        acknowledgements.append(acknowledgement)

    return acknowledgements


def validate_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError) as exc:
        raise NetworkIngestError(
            422,
            "INVALID_IDENTIFIER",
            f"{field} must be a UUID.",
        ) from exc
