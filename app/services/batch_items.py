from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Batch, InventoryTransaction


def update_transaction_references(
    db: Session,
    batch: Batch,
) -> None:
    if not batch.tally_reference:
        return
    rows = db.scalars(
        select(InventoryTransaction).where(InventoryTransaction.batch_id == batch.id)
    ).all()
    for row in rows:
        row.tally_reference = batch.tally_reference


def grouped_batch_items(
    batch: Batch,
) -> list[dict[str, object]]:
    grouped: dict[tuple[int, float], dict[str, object]] = {}
    for item in batch.items:
        product = item.serial.product
        rate = item.rate if item.rate is not None else product.default_rate
        row = grouped.setdefault(
            (product.id, float(rate or 0)),
            {
                "product": product,
                "quantity": 0,
                "rate": rate,
            },
        )
        row["quantity"] = int(row["quantity"]) + item.quantity
    return list(grouped.values())
