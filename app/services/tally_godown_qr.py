"""Create a franchise's QR stock from its Tally godown, item by item.

Setup-phase helper: read each stock item's closing quantity in the franchise's
Tally godown, preview one QR label per piece, then allocate the serials through
the normal QR workflow so Lite receives them as stock.

Tally is asked about one item at a time. The item name goes in a request
variable, never inside a TDL formula: an unquoted name is parsed as a formula
and makes Tally open a "Bad formula" dialog that blocks its gateway, and a
name containing a quote mark cannot be quoted. The read stops at the first error.
"""

from __future__ import annotations

import json
import math
import re
import time
import xml.etree.ElementTree as ET  # nosec B405 - builds requests only; responses use defusedxml
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    Batch,
    BatchItem,
    BatchStatus,
    BatchType,
    FranchiseNode,
    NetworkStock,
    Product,
    Serial,
    SerialStatus,
    utc_now,
)
from app.services.network_ingest import TALLY_OPENING_BATCH
from app.services.qr_allocation import MAX_ALLOCATION, allocate_qr_serials
from app.services.settings import get_setting, update_settings
from app.services.tally_masters import (
    TallyDataError,
    TallyStockItem,
    _build_collection_export,
    _direct_text,
    _local_tag,
    _post_read_request,
    fetch_tally_stock_items,
)
from app.services.tally_products import (
    _local_code,
    active_tally_source,
    import_blocker,
    import_tally_products,
)
from app.services.voucher import KG_UNITS, pack_grams

PLAN_KEY_PREFIX = "tally_godown_qr_plan:"
AUTO_SYNC_MINUTES_KEY = "tally_godown_sync_minutes"
AUTO_SYNC_LAST_AT_KEY = "tally_godown_sync_last_at"
AUTO_SYNC_LAST_RESULT_KEY = "tally_godown_sync_last_result"
DEFAULT_AUTO_SYNC_MINUTES = 30
MAX_AUTO_SYNC_MINUTES = 1440
# Tally can take minutes to build the first item list when company data is on a network share.
LIST_TIMEOUT_SECONDS = 900
ITEM_TIMEOUT_SECONDS = 20
ITEM_PAUSE_SECONDS = 0.1
# Labels that stand for no piece in Tally: replaced by a new label, or voided.
NEVER_IN_TALLY_STATUSES = {SerialStatus.INVALID.value, SerialStatus.REPLACED.value}
# Pieces that leave Tally's godown only once their voucher is posted.
POSTED_OUT_STATUSES = {SerialStatus.SOLD.value, SerialStatus.PURCHASE_RETURN.value}
POSTED_OUT_BATCH_TYPES = {BatchType.SALE.value, BatchType.PURCHASE_RETURN.value}
POSTED_BATCH_STATUSES = {BatchStatus.SYNCED.value, BatchStatus.CLOSED.value}
_QUANTITY = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*(.*)$")


@dataclass
class PlanLine:
    item: str
    unit: str
    tally_quantity: float
    labels: int
    already_held: int
    to_create: int
    note: str = ""


def _parse_quantity(raw: str) -> tuple[float, str]:
    match = _QUANTITY.match(raw or "")
    if not match:
        return 0.0, ""
    return float(match.group(1).replace(",", "")), match.group(2).strip()


def labels_for(item_name: str, quantity: float, unit: str) -> tuple[int, str]:
    """How many QR labels a Tally quantity needs, or 0 with the reason."""

    if quantity <= 0:
        return 0, "No stock in this godown."
    if unit.strip().upper() in KG_UNITS:
        grams = pack_grams(item_name)
        if grams is None:
            return 0, "Stock is in KG but the pack size is not in the item name."
        packets = quantity * 1000 / grams
        if not math.isclose(packets, round(packets), abs_tol=1e-6):
            return 0, f"{quantity:g} KG is not a whole number of {grams:g} g packets."
        return round(packets), f"{quantity:g} KG = {round(packets)} packets of {grams:g} g"
    if not math.isclose(quantity, round(quantity), abs_tol=1e-6):
        return 0, f"{quantity:g} {unit} is not a whole number of pieces."
    return round(quantity), ""


def _items_with_stock_xml(company_name: str) -> str:
    envelope, _ = _build_collection_export(
        "Setuora Item Closing List",
        "StockItem",
        ("Name", "ClosingBalance"),
        company_name=company_name,
    )
    return ET.tostring(envelope, encoding="unicode")


def _item_batches_xml(company_name: str, item_name: str) -> str:
    envelope, tdl_message = _build_collection_export(
        "Setuora Item Godown Batches",
        "Batch",
        ("Name", "GodownName", "ClosingBalance"),
        company_name=company_name,
    )
    # The name is a plain variable value, so quote marks in it are safe.
    static_variables = envelope.find("./BODY/DESC/STATICVARIABLES")
    ET.SubElement(static_variables, "SETUORAITEM").text = item_name
    variable = ET.Element("VARIABLE", {"NAME": "SetuoraItem"})
    ET.SubElement(variable, "TYPE").text = "String"
    tdl_message.insert(0, variable)
    ET.SubElement(tdl_message.find("COLLECTION"), "CHILDOF").text = "##SetuoraItem"
    return ET.tostring(envelope, encoding="unicode")


def fetch_godown_quantities(
    settings: dict[str, str], company_name: str, godown: str
) -> tuple[dict[str, tuple[float, str]], list[str]]:
    """Return {item: (quantity, unit)} for one godown and the names that were skipped."""

    _, root = _post_read_request(settings, _items_with_stock_xml(company_name), timeout=LIST_TIMEOUT_SECONDS
    )
    candidates: list[str] = []
    for node in root.iter():
        if _local_tag(node) != "STOCKITEM":
            continue
        name = (node.attrib.get("NAME") or "").strip() or _direct_text(node, "NAME")
        quantity, _ = _parse_quantity(_direct_text(node, "CLOSINGBALANCE"))
        if name and quantity != 0:
            candidates.append(name)
    quantities: dict[str, tuple[float, str]] = {}
    skipped: list[str] = []
    wanted = godown.strip().casefold()
    for name in candidates:
        try:
            _, batches = _post_read_request(
                settings, _item_batches_xml(company_name, name), timeout=ITEM_TIMEOUT_SECONDS
            )
        except TallyDataError as exc:
            raise TallyDataError(f"Tally stopped answering at item {name}: {exc}") from exc
        total, unit = 0.0, ""
        for batch in batches.iter():
            if _local_tag(batch) != "BATCH":
                continue
            if _direct_text(batch, "GODOWNNAME").strip().casefold() != wanted:
                continue
            quantity, batch_unit = _parse_quantity(_direct_text(batch, "CLOSINGBALANCE"))
            total += quantity
            unit = unit or batch_unit
        if total:
            quantities[name] = (total, unit)
        time.sleep(ITEM_PAUSE_SECONDS)
    return quantities, skipped


def _held_counts(db: Session, node: FranchiseNode) -> dict[int, int]:
    """Pieces Tally should show in this franchise's godown, per product.

    Damaged, missing and issued pieces are never posted out of Tally, and pieces
    sent to another franchise stay in this godown until the transfer's Stock
    Journal posts, so all of them still count. Sold and purchase-returned pieces
    count until their voucher posts. Counting less than Tally holds would make a
    reload create labels for pieces that already have one.
    """
    unposted_out = (
        select(BatchItem.serial_id)
        .join(Batch, Batch.id == BatchItem.batch_id)
        .where(
            Batch.batch_type.in_(POSTED_OUT_BATCH_TYPES),
            Batch.status.not_in(POSTED_BATCH_STATUSES),
        )
    )
    rows = db.execute(
        select(Serial.product_id, func.count(func.distinct(Serial.id)))
        .join(NetworkStock, NetworkStock.serial_id == Serial.id)
        .where(
            NetworkStock.current_franchise_id == node.id,
            NetworkStock.status.not_in(NEVER_IN_TALLY_STATUSES),
            or_(
                NetworkStock.status.not_in(POSTED_OUT_STATUSES),
                Serial.id.in_(unposted_out),
            ),
        )
        .group_by(Serial.product_id)
    ).all()
    held = dict(rows)
    godown = (node.tally_godown_name or "").strip()
    if godown:
        moved_out = db.execute(
            select(Serial.product_id, func.count(func.distinct(Serial.id)))
            .join(NetworkStock, NetworkStock.serial_id == Serial.id)
            .join(BatchItem, BatchItem.serial_id == Serial.id)
            .join(Batch, Batch.id == BatchItem.batch_id)
            .where(
                NetworkStock.current_franchise_id != node.id,
                Batch.batch_type == BatchType.STOCK_TRANSFER.value,
                Batch.status.not_in(POSTED_BATCH_STATUSES),
                func.upper(Batch.tally_source_location) == godown.upper(),
            )
            .group_by(Serial.product_id)
        ).all()
        for product_id, count in moved_out:
            held[product_id] = held.get(product_id, 0) + count
    return held


def _node_products_by_tally_name(db: Session, node: FranchiseNode) -> dict[str, Product]:
    products = db.scalars(
        select(Product).where(Product.product_code.startswith(f"{node.code}:"))
    ).all()
    return {(p.tally_stock_item_name or "").casefold(): p for p in products if p.active}


def build_plan(
    db: Session,
    node: FranchiseNode,
    quantities: dict[str, tuple[float, str]],
    items: list[TallyStockItem],
    skipped: list[str],
) -> dict:
    """Preview lines; labels already held at the franchise are not created again."""

    by_name = {item.name.casefold(): item for item in items}
    products = _node_products_by_tally_name(db, node)
    held = _held_counts(db, node)
    lines: list[PlanLine] = []
    for name, (quantity, unit) in sorted(quantities.items(), key=lambda kv: kv[0].casefold()):
        item = by_name.get(name.casefold())
        unit = unit or (item.unit if item else "")
        labels, note = labels_for(name, quantity, unit)
        blocker = import_blocker(item) if item else "Stock item details were not found in Tally."
        if blocker and name.casefold() not in products:
            labels, note = 0, blocker
        product = products.get(name.casefold())
        already = held.get(product.id, 0) if product else 0
        if labels and already > labels:
            note = (
                f"MISMATCH: Master/Lite hold {already}, Tally has {labels}. "
                "Check transfers out of this godown in Tally."
            )
        lines.append(
            PlanLine(
                item=name,
                unit=unit,
                tally_quantity=quantity,
                labels=labels,
                already_held=already,
                to_create=min(max(labels - already, 0), MAX_ALLOCATION),
                note=note,
            )
        )
    in_tally = {name.casefold() for name in quantities}
    for key, product in sorted(products.items()):
        already = held.get(product.id, 0)
        if already and key not in in_tally:
            lines.append(
                PlanLine(
                    item=product.tally_stock_item_name or product.product_name,
                    unit=product.unit or "",
                    tally_quantity=0,
                    labels=0,
                    already_held=already,
                    to_create=0,
                    note=f"MISMATCH: Master/Lite hold {already}, Tally has none in this godown.",
                )
            )
    return {
        "token": str(uuid4()),
        "franchise": node.public_id,
        "godown": node.tally_godown_name,
        "created_at": utc_now().isoformat(),
        "skipped": skipped,
        "mismatches": sum(1 for line in lines if line.note.startswith("MISMATCH")),
        "lines": [asdict(line) for line in lines],
    }


def save_plan(db: Session, node: FranchiseNode, plan: dict) -> None:
    update_settings(db, {PLAN_KEY_PREFIX + node.public_id: json.dumps(plan)})


def load_plan(db: Session, node: FranchiseNode) -> dict | None:
    raw = get_setting(db, PLAN_KEY_PREFIX + node.public_id)
    if not raw:
        return None
    try:
        plan = json.loads(raw)
    except ValueError:
        return None
    return plan if isinstance(plan, dict) and plan.get("franchise") == node.public_id else None


def read_godown_stock(
    settings: dict[str, str], company_name: str, godown: str
) -> tuple[dict[str, tuple[float, str]], list[TallyStockItem], list[str]]:
    """Read from Tally only (no database), so callers may run it in a worker thread."""

    if not godown.strip():
        raise TallyDataError("Set this franchise's Tally godown first.")
    if not company_name.strip():
        raise TallyDataError("Choose a Tally company in Settings first.")
    quantities, skipped = fetch_godown_quantities(settings, company_name, godown)
    wanted = {name.casefold() for name in quantities}
    items = [
        item
        for item in fetch_tally_stock_items(settings, company_name)
        if item.name.casefold() in wanted
    ]
    return quantities, items, skipped


def plan_from_stock(
    db: Session,
    node: FranchiseNode,
    quantities: dict[str, tuple[float, str]],
    items: list[TallyStockItem],
    skipped: list[str],
) -> dict:
    plan = build_plan(db, node, quantities, items, skipped)
    plan["_items"] = [asdict(item) for item in items]
    return plan


def create_from_plan(
    db: Session, node: FranchiseNode, plan: dict, token: str, *, created_by_id: int | None
) -> tuple[int, int]:
    """Import the products and allocate each line's serials; returns (items, serials).

    Each item is committed on its own with an allocation ID derived from the plan
    token, so a repeated or interrupted submit never creates the same labels twice.
    """

    if not token or plan.get("token") != token:
        raise ValueError("This preview is out of date. Load the stock from Tally again.")
    lines = [line for line in plan.get("lines", []) if int(line.get("to_create") or 0) > 0]
    if not lines:
        return 0, 0
    wanted = {line["item"].casefold() for line in lines}
    items = [
        TallyStockItem(**data)
        for data in plan.get("_items", [])
        if data.get("name", "").casefold() in wanted
    ]
    import_tally_products(db, nodes=[node], items=items)
    db.commit()
    products = _node_products_by_tally_name(db, node)
    done_items = done_serials = 0
    for line in lines:
        product = products.get(line["item"].casefold())
        if product is None:
            continue
        serials = allocate_qr_serials(
            db,
            node=node,
            product=product,
            product_code=_local_code(node, product),
            quantity=int(line["to_create"]),
            # Marks the stock as already in Tally so Lite's stock-in posts no Purchase.
            product_batch_number=TALLY_OPENING_BATCH,
            allocation_id=str(uuid5(NAMESPACE_URL, f"setuora-godown-qr:{token}:{line['item']}")),
            created_by_id=created_by_id,
        )
        db.commit()
        done_items += 1
        done_serials += len(serials)
    return done_items, done_serials


def auto_sync_interval_minutes(db: Session) -> int:
    try:
        minutes = int(get_setting(db, AUTO_SYNC_MINUTES_KEY, str(DEFAULT_AUTO_SYNC_MINUTES)))
    except ValueError:
        return DEFAULT_AUTO_SYNC_MINUTES
    return max(0, min(minutes, MAX_AUTO_SYNC_MINUTES))


def save_auto_sync_interval(db: Session, raw: str) -> None:
    value = raw.strip()
    if not value.isdigit() or int(value) > MAX_AUTO_SYNC_MINUTES:
        raise ValueError(f"Godown stock check must be 0 to {MAX_AUTO_SYNC_MINUTES} minutes.")
    update_settings(db, {AUTO_SYNC_MINUTES_KEY: str(int(value))})


def auto_sync_last_at(db: Session) -> datetime | None:
    raw = get_setting(db, AUTO_SYNC_LAST_AT_KEY, "")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def auto_sync_franchises(db: Session) -> list[FranchiseNode]:
    """Franchises whose first godown load is done: later stock arrives automatically.

    The first load of a franchise stays a reviewed, manual step.
    """
    loaded = exists().where(
        NetworkStock.origin_franchise_id == FranchiseNode.id,
        NetworkStock.serial_id == Serial.id,
        Serial.product_batch_number == TALLY_OPENING_BATCH,
    )
    return list(
        db.scalars(
            select(FranchiseNode)
            .where(
                FranchiseNode.active.is_(True),
                FranchiseNode.tally_godown_name.is_not(None),
                FranchiseNode.tally_godown_name != "",
                loaded,
            )
            .order_by(FranchiseNode.code)
        )
    )


def _record_auto_sync(result: str) -> None:
    with SessionLocal() as db:
        update_settings(
            db, {AUTO_SYNC_LAST_AT_KEY: utc_now().isoformat(), AUTO_SYNC_LAST_RESULT_KEY: result}
        )


def run_godown_auto_sync(*, force: bool = False) -> str | None:
    """Create QR stock for pieces newly added to each franchise's Tally godown."""

    with SessionLocal() as db:
        minutes = auto_sync_interval_minutes(db)
        last = auto_sync_last_at(db)
        if not force and (
            minutes <= 0 or (last is not None and utc_now() - last < timedelta(minutes=minutes))
        ):
            return None
        targets = [
            (node.id, node.code, node.tally_godown_name or "") for node in auto_sync_franchises(db)
        ]
        if not targets:
            return None
        try:
            config, company = active_tally_source(db)
        except TallyDataError as exc:
            _record_auto_sync(f"Failed: {exc}")
            return None
    results: list[str] = []
    for node_id, code, godown in targets:
        started = utc_now()
        try:
            quantities, items, skipped = read_godown_stock(config, company, godown)
        except TallyDataError as exc:
            results.append(f"{code}: Tally did not answer ({exc})")
            continue
        with SessionLocal() as db:
            # A voucher posted while Tally was being read may be missing from what
            # was read, which would look like new stock. Read again next run.
            if db.scalar(select(Batch.id).where(Batch.synced_at >= started).limit(1)) is not None:
                results.append(f"{code}: Tally changed during the check; checking again next run")
                continue
            node = db.get(FranchiseNode, node_id)
            plan = plan_from_stock(db, node, quantities, items, skipped)
            done_items, serials = create_from_plan(
                db, node, plan, plan["token"], created_by_id=None
            )
            text = f"{code}: {serials} new QR labels for {done_items} items"
            if plan["mismatches"]:
                text += f"; {plan['mismatches']} items hold more than Tally shows (check on Load from Tally)"
            results.append(text)
    result = ". ".join(results) + "."
    _record_auto_sync(result)
    return result
