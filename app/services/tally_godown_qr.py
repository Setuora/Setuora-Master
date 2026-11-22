"""Create a franchise's QR stock from its Tally godown, item by item.

Setup-phase helper: read each stock item's closing quantity in the franchise's
Tally godown, preview one QR label per piece, then allocate the serials through
the normal QR workflow so Lite receives them as stock.

Tally is asked about one item at a time with the item name passed as a quoted
TDL string. An unquoted name is parsed as a formula and makes Tally open a
"Bad formula" dialog that blocks its gateway, so names that cannot be quoted
safely are skipped and the read stops at the first error.
"""

from __future__ import annotations

import json
import math
import re
import time
import xml.etree.ElementTree as ET  # nosec B405 - builds requests only; responses use defusedxml
from dataclasses import asdict, dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FranchiseNode, NetworkStock, Product, Serial, SerialStatus, utc_now
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
from app.services.tally_products import _local_code, import_blocker, import_tally_products
from app.services.voucher import KG_UNITS, pack_grams

PLAN_KEY_PREFIX = "tally_godown_qr_plan:"
# Tally can take minutes to build the first item list when company data is on a network share.
LIST_TIMEOUT_SECONDS = 900
ITEM_TIMEOUT_SECONDS = 20
ITEM_PAUSE_SECONDS = 0.1
# Serials that no longer count as stock held at the franchise.
NOT_HELD_STATUSES = {
    SerialStatus.SOLD.value,
    SerialStatus.DAMAGED.value,
    SerialStatus.MISSING.value,
    SerialStatus.INVALID.value,
    SerialStatus.REPLACED.value,
    SerialStatus.PURCHASE_RETURN.value,
    SerialStatus.IN_TRANSIT.value,
}
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
    collection = tdl_message.find("COLLECTION")
    # A quoted TDL string; ElementTree escapes the XML characters.
    ET.SubElement(collection, "CHILDOF").text = f'"{item_name}"'
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
        if '"' in name or "\\" in name:
            skipped.append(name)
            continue
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
    rows = db.execute(
        select(Serial.product_id, func.count(NetworkStock.id))
        .join(NetworkStock, NetworkStock.serial_id == Serial.id)
        .where(
            NetworkStock.current_franchise_id == node.id,
            NetworkStock.status.not_in(NOT_HELD_STATUSES),
        )
        .group_by(Serial.product_id)
    ).all()
    return dict(rows)


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
