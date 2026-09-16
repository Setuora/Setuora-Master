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
    grouped: dict[tuple[int, float, float], dict[str, object]] = {}
    for item in batch.items:
        product = item.serial.product
        rate = item.rate if item.rate is not None else product.default_rate
        discount = (
            item.sales_discount_rate
            if item.sales_discount_rate is not None
            else product.sales_discount_rate
        )
        row = grouped.setdefault(
            (product.id, float(rate or 0), float(discount or 0)),
            {
                "product": product,
                "quantity": 0,
                "rate": rate,
                "sales_discount_rate": discount,
            },
        )
        row["quantity"] = int(row["quantity"]) + item.quantity
    return list(grouped.values())
