from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE
from app.models import (
    Batch,
    BatchItem,
    BatchStatus,
    BatchType,
    Product,
    Serial,
    SerialStatus,
    StorageLocation,
    User,
)
from app.security import create_session_token


def authenticate_client(client, user_id: int) -> None:
    """Replace any prior TestClient session with the selected test user."""
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, create_session_token(user_id))


def create_batch(
    db: Session,
    user: User,
    batch_type: BatchType,
    party_name: str | None,
    notes: str | None,
    reason_code: str | None = None,
    **values,
) -> Batch:
    next_id = (db.scalar(select(func.count(Batch.id))) or 0) + 1
    batch = Batch(
        batch_number=f"TEST-{batch_type.value}-{next_id:04d}",
        batch_type=batch_type.value,
        party_name=party_name,
        notes=notes,
        reason_code=reason_code,
        user_id=user.id,
        **values,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def generate_serials(
    db: Session,
    product: Product,
    quantity: int,
    *,
    initial_status: SerialStatus = SerialStatus.GENERATED,
) -> list[Serial]:
    start = db.scalar(select(func.count(Serial.id))) or 0
    serials = [
        Serial(
            serial_number=f"{product.product_code}-TEST-{start + offset + 1:05d}",
            product_id=product.id,
            status=initial_status.value,
        )
        for offset in range(quantity)
    ]
    db.add_all(serials)
    db.commit()
    return serials


def add_serial_to_batch(
    db: Session,
    batch: Batch,
    _user: User,
    serial_number: str,
) -> BatchItem:
    serial = db.scalar(select(Serial).where(Serial.serial_number == serial_number))
    if serial is None:
        raise ValueError(f"Unknown serial: {serial_number}")
    item = BatchItem(
        batch_id=batch.id,
        serial_id=serial.id,
        quantity=1,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def apply_batch_statuses(
    db: Session,
    batch: Batch,
    _user: User,
) -> None:
    batch.status = BatchStatus.SUBMITTED.value
    batch.submitted_at = datetime.now(UTC)
    db.commit()


def update_batch_item_rate(
    db: Session,
    batch: Batch,
    item_id: int,
    rate: float,
) -> None:
    item = db.get(BatchItem, item_id)
    if item is None or item.batch_id != batch.id:
        raise ValueError("Batch item not found")
    item.rate = rate
    db.commit()


def verify_pending_items_on_shelf(
    db: Session,
    *,
    batch: Batch,
    location: StorageLocation,
    user: User,
) -> None:
    now = datetime.now(UTC)
    for item in batch.items:
        item.shelf_location_id = location.id
        item.shelf_verified_by_id = user.id
        item.shelf_verified_at = now
    db.commit()
