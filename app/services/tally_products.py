"""Import central Tally stock items as franchise products and keep them in step.

Products are created on Master and delivered to each franchise's Lite with a
PRODUCT_UPSERT command. Live sync re-reads the Tally item master on the
administrator's schedule and sends identity changes the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    FranchiseNode,
    NetworkStock,
    Product,
    ProductIdentityHistory,
    Serial,
    utc_now,
)
from app.services.network_ingest import (
    PRODUCT_UPSERT_COMMAND,
    _namespaced_code,
    queue_node_command,
)
from app.services.settings import (
    company_config,
    get_active_company,
    get_setting,
    update_settings,
)
from app.services.tally_masters import TallyDataError, TallyStockItem, fetch_tally_stock_items

SYNC_MINUTES_KEY = "tally_product_sync_minutes"
SYNC_LAST_AT_KEY = "tally_product_sync_last_at"
SYNC_LAST_RESULT_KEY = "tally_product_sync_last_result"
DEFAULT_SYNC_MINUTES = 15
MAX_SYNC_MINUTES = 1440
# Franchise codes are at most 20 characters, so a 59-character local code keeps
# the namespaced Master code within 80 characters and reversible.
MAX_LOCAL_CODE_LENGTH = 59


@dataclass(frozen=True)
class ImportSummary:
    created: int
    existing: int
    skipped: int


@dataclass(frozen=True)
class SyncSummary:
    checked: int
    updated: int
    missing: int


def suggest_product_code(item: TallyStockItem) -> str:
    """Use a code-like Tally alias when there is one, otherwise the item name."""

    candidates = [item.alias] if item.alias and not any(c.isspace() for c in item.alias) else []
    for candidate in [*candidates, item.name]:
        code = re.sub(r"[^A-Z0-9]+", "-", candidate.upper()).strip("-")
        code = code[:MAX_LOCAL_CODE_LENGTH].strip("-")
        if code:
            return code
    return ""


def import_blocker(item: TallyStockItem) -> str:
    """Why an item cannot be imported, or an empty string when it can."""

    if item.gst_rate is None:
        return "Set the GST rate on this stock item in Tally."
    if not 0 <= item.gst_rate <= 100:
        return "Tally GST rate is outside 0 to 100%."
    if len(item.name) > 180 or len(item.hsn) > 40 or len(item.unit) > 40:
        return "Name, HSN, or unit is too long."
    if not suggest_product_code(item):
        return "No product code can be made from this name."
    return ""


def _local_code(node: FranchiseNode, product: Product) -> str:
    prefix = f"{node.code}:"
    if not product.product_code.startswith(prefix):
        raise ValueError(f"Product {product.product_code} does not belong to {node.code}.")
    return product.product_code[len(prefix) :]


def _held_local_codes(db: Session, node: FranchiseNode) -> dict[str, str]:
    """Local codes of products this franchise holds only through transfers.

    A transferred unit keeps its origin's product code on the receiving Lite, so
    reusing that code makes an import update the Lite product instead of
    duplicating it.
    """

    rows = db.execute(
        select(Product.tally_stock_item_name, Product.product_code)
        .join(Serial, Serial.product_id == Product.id)
        .join(NetworkStock, NetworkStock.serial_id == Serial.id)
        .where(NetworkStock.current_franchise_id == node.id)
        .order_by(Product.id)
    ).all()
    held: dict[str, str] = {}
    for tally_name, product_code in rows:
        local_code = product_code.split(":", 1)[-1]
        if tally_name and len(local_code) <= MAX_LOCAL_CODE_LENGTH:
            held.setdefault(tally_name.casefold(), local_code)
    return held


def queue_product_upsert(db: Session, node: FranchiseNode, product: Product) -> None:
    queue_node_command(
        db,
        target=node,
        command_type=PRODUCT_UPSERT_COMMAND,
        payload={
            "version": 1,
            "franchise_code": node.code,
            "product": {
                "product_code": _local_code(node, product),
                "product_name": product.product_name,
                "tally_stock_item_name": product.tally_stock_item_name,
                "alternate_tally_stock_item_name": product.alternate_tally_stock_item_name,
                "hsn": product.hsn,
                "gst_rate": float(product.gst_rate or 0),
                "unit": product.unit,
                "default_rate": float(product.default_rate or 0),
                "sales_discount_rate": float(product.sales_discount_rate or 0),
                "active": bool(product.active),
            },
        },
    )


def import_tally_products(
    db: Session,
    *,
    nodes: list[FranchiseNode],
    items: list[TallyStockItem],
) -> ImportSummary:
    """Create the selected items for each franchise in the caller's transaction."""

    now = utc_now()
    created = existing = skipped = 0
    for node in nodes:
        node_products = db.scalars(
            select(Product).where(Product.product_code.startswith(f"{node.code}:"))
        ).all()
        by_tally_name = {
            product.tally_stock_item_name.casefold(): product for product in node_products
        }
        by_code = {product.product_code: product for product in node_products}
        held_codes = _held_local_codes(db, node)
        for item in items:
            if import_blocker(item):
                skipped += 1
                continue
            code = _namespaced_code(
                node, held_codes.get(item.name.casefold()) or suggest_product_code(item)
            )
            product = by_code.get(code) or by_tally_name.get(item.name.casefold())
            if product is not None:
                # Link a product created by hand or an earlier import so live
                # sync maintains it from now on.
                product.tally_guid = product.tally_guid or item.guid or None
                product.tally_synced_at = product.tally_synced_at or now
                existing += 1
                continue
            product = Product(
                product_code=code,
                product_name=item.name,
                hsn=item.hsn,
                gst_rate=float(item.gst_rate or 0),
                unit=item.unit or "Pcs",
                default_rate=item.standard_rate or 0,
                sales_discount_rate=0,
                tally_stock_item_name=item.name,
                tally_guid=item.guid or None,
                tally_synced_at=now,
                active=True,
            )
            db.add(product)
            db.flush()
            by_code[code] = product
            by_tally_name[item.name.casefold()] = product
            queue_product_upsert(db, node, product)
            created += 1
    db.flush()
    return ImportSummary(created=created, existing=existing, skipped=skipped)


def apply_tally_stock_items(db: Session, items: list[TallyStockItem]) -> SyncSummary:
    """Bring Tally-linked products in line with the current Tally item master."""

    now = utc_now()
    by_guid = {item.guid: item for item in items if item.guid}
    by_name = {item.name.casefold(): item for item in items}
    nodes = {node.code: node for node in db.scalars(select(FranchiseNode))}
    linked = db.scalars(
        select(Product).where(Product.tally_synced_at.is_not(None)).order_by(Product.id)
    ).all()
    updated = missing = 0
    for product in linked:
        item = by_guid.get(product.tally_guid or "") or by_name.get(
            product.tally_stock_item_name.casefold()
        )
        if item is None:
            missing += 1
            continue
        renamed_display = product.product_name == product.tally_stock_item_name
        target = {
            "product_name": item.name if renamed_display else product.product_name,
            "tally_stock_item_name": item.name,
            "hsn": item.hsn or product.hsn,
            "unit": item.unit or product.unit,
            "gst_rate": product.gst_rate if import_blocker(item) else float(item.gst_rate or 0),
        }
        changed = (
            any(
                getattr(product, field) != value
                for field, value in target.items()
                if field != "gst_rate"
            )
            or abs(float(product.gst_rate or 0) - float(target["gst_rate"])) > 0.0001
        )
        product.tally_guid = product.tally_guid or item.guid or None
        product.tally_synced_at = now
        if not changed:
            continue
        db.add(
            ProductIdentityHistory(
                product_id=product.id,
                product_name=product.product_name,
                tally_stock_item_name=product.tally_stock_item_name,
                hsn=product.hsn,
                unit=product.unit,
                gst_rate=float(product.gst_rate or 0),
                replaced_at=now,
            )
        )
        for field, value in target.items():
            setattr(product, field, value)
        node = nodes.get(product.product_code.split(":", 1)[0])
        if node is not None:
            queue_product_upsert(db, node, product)
        updated += 1
    db.flush()
    return SyncSummary(checked=len(linked), updated=updated, missing=missing)


def sync_interval_minutes(db: Session) -> int:
    try:
        minutes = int(get_setting(db, SYNC_MINUTES_KEY, str(DEFAULT_SYNC_MINUTES)))
    except ValueError:
        return DEFAULT_SYNC_MINUTES
    return max(0, min(minutes, MAX_SYNC_MINUTES))


def save_sync_interval(db: Session, raw: str) -> int:
    value = raw.strip()
    if not value.isdigit() or int(value) > MAX_SYNC_MINUTES:
        raise ValueError(f"Live sync interval must be 0 to {MAX_SYNC_MINUTES} minutes.")
    update_settings(db, {SYNC_MINUTES_KEY: str(int(value))})
    return int(value)


def last_sync_at(db: Session) -> datetime | None:
    raw = get_setting(db, SYNC_LAST_AT_KEY, "")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def sync_is_due(db: Session, now: datetime | None = None) -> bool:
    minutes = sync_interval_minutes(db)
    if minutes <= 0:
        return False
    if db.scalar(select(Product.id).where(Product.tally_synced_at.is_not(None)).limit(1)) is None:
        return False
    last = last_sync_at(db)
    return last is None or (now or utc_now()) - last >= timedelta(minutes=minutes)


def active_tally_source(db: Session) -> tuple[dict[str, str], str]:
    company = get_active_company(db)
    if company is None:
        raise TallyDataError("Add and activate a Tally company in Tally settings first.")
    config = company_config(company)
    return config, config.get("company_name", "")


def run_tally_product_sync() -> SyncSummary:
    """Read Tally outside any database transaction, then apply the changes."""

    try:
        with SessionLocal() as db:
            config, tally_company = active_tally_source(db)
        items = fetch_tally_stock_items(config, tally_company)
    except TallyDataError as exc:
        _record_sync_result(f"Failed: {exc}")
        raise
    with SessionLocal() as db:
        summary = apply_tally_stock_items(db, items)
        _record_sync_result(
            f"Checked {summary.checked}, updated {summary.updated}, "
            f"missing in Tally {summary.missing}",
            db=db,
        )
        db.commit()
    return summary


def _record_sync_result(message: str, *, db: Session | None = None) -> None:
    values = {SYNC_LAST_AT_KEY: utc_now().isoformat(), SYNC_LAST_RESULT_KEY: message[:500]}
    if db is not None:
        update_settings(db, values, commit=False)
        return
    with SessionLocal() as session:
        update_settings(session, values)
