"""Master-owned QR identity allocation and durable delivery to one Lite node."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    FranchiseNode,
    NetworkStock,
    NodeCommand,
    Product,
    QrAllocation,
    Serial,
    SerialStatus,
    WarehouseLevel,
)
from app.services.network_ingest import _namespaced_code, queue_node_command

MAX_ALLOCATION = 5000
COMMAND_ITEMS = 500


def new_qr_serial() -> str:
    """An opaque, globally unique serial that does not depend on local counters."""

    return f"SQR-{uuid4().hex.upper()}"


def get_or_create_franchise_product(
    db: Session,
    node: FranchiseNode,
    *,
    product_code: str,
    product_name: str = "",
    tally_stock_item_name: str = "",
    hsn: str = "",
    gst_rate: float = 0,
    unit: str = "Pcs",
    default_rate: float = 0,
    sales_discount_rate: float = 0,
) -> Product:
    local_code = product_code.strip().upper()
    if not local_code or len(local_code) > 80 or any(ord(char) < 32 for char in local_code):
        raise ValueError("Product code must be 1 to 80 printable characters.")
    key = _namespaced_code(node, local_code)
    product = db.scalar(select(Product).where(Product.product_code == key))
    if product is not None:
        if not product.active:
            raise ValueError("This product is inactive.")
        return product
    name = product_name.strip()
    tally_name = tally_stock_item_name.strip() or name
    unit = unit.strip()
    if not name or len(name) > 180 or not tally_name or len(tally_name) > 180:
        raise ValueError("A new product needs a name and Tally item name of 180 characters or fewer.")
    if not unit or len(unit) > 40 or len(hsn.strip()) > 40:
        raise ValueError("Unit and HSN are too long or missing.")
    if (
        not all(math.isfinite(value) for value in (gst_rate, sales_discount_rate, default_rate))
        or not 0 <= gst_rate <= 100
        or not 0 <= sales_discount_rate <= 100
        or default_rate < 0
    ):
        raise ValueError("GST, discount, or rate is outside its allowed range.")
    product = Product(
        product_code=key,
        product_name=name,
        hsn=hsn.strip(),
        gst_rate=gst_rate,
        unit=unit,
        default_rate=default_rate,
        sales_discount_rate=sales_discount_rate,
        tally_stock_item_name=tally_name,
        active=True,
    )
    db.add(product)
    db.flush()
    return product


def allocate_qr_serials(
    db: Session,
    *,
    node: FranchiseNode,
    product: Product,
    product_code: str,
    quantity: int,
    product_batch_number: str | None = None,
    mfg_date: date | None = None,
    expiry_date: date | None = None,
    warehouse: str | None = None,
    warehouse_level: str = WarehouseLevel.COMPANY_WAREHOUSE.value,
    allocation_id: str | None = None,
    created_by_id: int | None = None,
) -> list[str]:
    """Create stock identities and commands in the caller's single transaction."""

    if not node.active:
        raise ValueError("Enable this franchise before creating QR codes.")
    if not 1 <= quantity <= MAX_ALLOCATION:
        raise ValueError(f"Create between 1 and {MAX_ALLOCATION:,} QR codes at a time.")
    if not product.active or product.product_code != _namespaced_code(node, product_code.strip().upper()):
        raise ValueError("Product does not belong to this franchise or is inactive.")
    if not (product.tally_stock_item_name or "").strip():
        raise ValueError("Set a Tally stock item name for this product before creating QR codes.")
    if mfg_date and expiry_date and expiry_date <= mfg_date:
        raise ValueError("Expiry date must be after manufacture date.")
    if product_batch_number and len(product_batch_number.strip()) > 80:
        raise ValueError("Product batch number must be 80 characters or fewer.")
    if warehouse and len(warehouse.strip()) > 80:
        raise ValueError("Warehouse must be 80 characters or fewer.")
    try:
        warehouse_level = WarehouseLevel(warehouse_level).value
    except ValueError as exc:
        raise ValueError("Choose a valid warehouse level.") from exc

    local_code = product_code.strip().upper()
    batch_number = product_batch_number.strip().upper() if product_batch_number else None
    warehouse_name = warehouse.strip().upper() if warehouse else node.tally_godown_name
    try:
        allocation_id = str(UUID(allocation_id)) if allocation_id else str(uuid4())
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid allocation request ID.") from exc
    request_json = json.dumps(
        {
            "node_id": node.id,
            "product_id": product.id,
            "quantity": quantity,
            "product_batch_number": batch_number,
            "mfg_date": mfg_date.isoformat() if mfg_date else None,
            "expiry_date": expiry_date.isoformat() if expiry_date else None,
            "warehouse": warehouse_name,
            "warehouse_level": warehouse_level,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    existing = db.scalar(select(QrAllocation).where(QrAllocation.public_id == allocation_id))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ValueError("This allocation request ID was already used for different QR codes.")
        commands = db.scalars(
            select(NodeCommand)
            .where(
                NodeCommand.target_franchise_id == node.id,
                NodeCommand.command_type == "QR_ALLOCATED",
                NodeCommand.payload_json.like(f'%"allocation_id":"{allocation_id}"%'),
            )
            .order_by(NodeCommand.id)
        ).all()
        serial_numbers = [
            row["serial_number"]
            for command in commands
            for row in json.loads(command.payload_json)["serials"]
        ]
        if len(serial_numbers) != quantity:
            raise ValueError("This QR allocation is incomplete. Check its command records.")
        return serial_numbers
    db.add(
        QrAllocation(
            public_id=allocation_id,
            franchise_id=node.id,
            product_id=product.id,
            request_hash=request_hash,
            quantity=quantity,
            created_by_id=created_by_id,
        )
    )
    db.flush()
    part_count = (quantity + COMMAND_ITEMS - 1) // COMMAND_ITEMS
    all_serials: list[str] = []
    product_payload = {
        "product_code": local_code,
        "product_name": product.product_name,
        "tally_stock_item_name": product.tally_stock_item_name,
        "alternate_tally_stock_item_name": product.alternate_tally_stock_item_name,
        "hsn": product.hsn,
        "gst_rate": float(product.gst_rate or 0),
        "unit": product.unit,
        "default_rate": float(product.default_rate or 0),
        "sales_discount_rate": float(product.sales_discount_rate or 0),
        "active": bool(product.active),
    }
    for part_index in range(part_count):
        count = min(COMMAND_ITEMS, quantity - len(all_serials))
        serials = [
            Serial(
                serial_number=new_qr_serial(),
                product_id=product.id,
                status=SerialStatus.GENERATED.value,
                active=True,
                product_batch_number=batch_number,
                mfg_date=mfg_date,
                expiry_date=expiry_date,
                warehouse=warehouse_name,
                warehouse_level=warehouse_level,
            )
            for _ in range(count)
        ]
        db.add_all(serials)
        db.flush()
        db.add_all(
            NetworkStock(
                serial_id=serial.id,
                current_franchise_id=node.id,
                origin_franchise_id=node.id,
                status=SerialStatus.GENERATED.value,
                last_event_id=None,
            )
            for serial in serials
        )
        payload = {
            "version": 1,
            "franchise_code": node.code,
            "allocation_id": allocation_id,
            "part_number": part_index + 1,
            "part_count": part_count,
            "product": product_payload,
            "serials": [
                {
                    "serial_number": serial.serial_number,
                    "status": SerialStatus.GENERATED.value,
                    "product_batch_number": serial.product_batch_number,
                    "mfg_date": serial.mfg_date.isoformat() if serial.mfg_date else None,
                    "expiry_date": serial.expiry_date.isoformat() if serial.expiry_date else None,
                    "warehouse": serial.warehouse,
                    "warehouse_level": serial.warehouse_level,
                }
                for serial in serials
            ],
        }
        queue_node_command(db, target=node, command_type="QR_ALLOCATED", payload=payload)
        all_serials.extend(serial.serial_number for serial in serials)
    db.flush()
    return all_serials
