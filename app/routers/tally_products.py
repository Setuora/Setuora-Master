"""Import central Tally stock items as franchise products and run live sync."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import ADMIN_ROLES, require_user
from app.database import get_db
from app.models import FranchiseNode, Product
from app.services.settings import get_setting
from app.services.tally_masters import TallyDataError, fetch_tally_stock_items
from app.services.tally_products import (
    MAX_SYNC_MINUTES,
    SYNC_LAST_RESULT_KEY,
    active_tally_source,
    import_blocker,
    import_tally_products,
    last_sync_at,
    run_tally_product_sync,
    save_sync_interval,
    suggest_product_code,
    sync_interval_minutes,
)
from app.templates import templates

router = APIRouter(prefix="/network/products", tags=["master-products"])

# Every ticked item is one form field; allow a full item master to be imported.
MAX_IMPORT_FORM_FIELDS = 20000


def _redirect(path: str = "/network/products", **params: object) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value != ""})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _active_nodes(db: Session) -> list[FranchiseNode]:
    return list(
        db.scalars(
            select(FranchiseNode).where(FranchiseNode.active.is_(True)).order_by(FranchiseNode.code)
        )
    )


async def _fetch_items(db: Session):
    config, tally_company = active_tally_source(db)
    return await asyncio.to_thread(fetch_tally_stock_items, config, tally_company)


@router.get("")
def products_page(
    request: Request,
    message: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    products = db.scalars(select(Product).order_by(Product.product_code)).all()
    return templates.TemplateResponse(
        request,
        "master/products.html",
        {
            "request": request,
            "user": user,
            "products": products,
            "message": message,
            "error": error,
            "sync_minutes": sync_interval_minutes(db),
            "max_sync_minutes": MAX_SYNC_MINUTES,
            "last_sync_at": last_sync_at(db),
            "last_sync_result": get_setting(db, SYNC_LAST_RESULT_KEY, ""),
        },
    )


@router.get("/import")
async def import_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, roles=ADMIN_ROLES)
    try:
        items = await _fetch_items(db)
    except TallyDataError as exc:
        return _redirect(error=str(exc))
    nodes = _active_nodes(db)
    known = {product.tally_stock_item_name.casefold() for product in db.scalars(select(Product))}
    rows = [
        {
            "item": item,
            "code": suggest_product_code(item),
            "blocker": import_blocker(item),
            "known": item.name.casefold() in known,
        }
        for item in items
    ]
    return templates.TemplateResponse(
        request,
        "master/product_import.html",
        {"request": request, "user": user, "rows": rows, "nodes": nodes},
    )


@router.post("/import")
async def import_selected(request: Request, db: Session = Depends(get_db)):
    require_user(request, db, roles=ADMIN_ROLES)
    form = await request.form(max_fields=MAX_IMPORT_FORM_FIELDS)
    selected = {str(value) for value in form.getlist("item")}
    franchise_ids = {str(value) for value in form.getlist("franchise")}
    if not selected:
        return _redirect(error="Tick at least one Tally stock item to import.")
    nodes = [node for node in _active_nodes(db) if node.public_id in franchise_ids]
    if not nodes:
        return _redirect(error="Tick at least one active franchise to receive the products.")
    try:
        # Re-read Tally so the import uses current item details, not form values.
        items = await _fetch_items(db)
    except TallyDataError as exc:
        return _redirect(error=str(exc))
    chosen = [item for item in items if item.name in selected]
    summary = import_tally_products(db, nodes=nodes, items=chosen)
    db.commit()
    parts = [f"Imported {summary.created} product{'s' if summary.created != 1 else ''}"]
    if summary.existing:
        parts.append(f"{summary.existing} already existed and are now linked to Tally")
    if summary.skipped:
        parts.append(f"{summary.skipped} skipped because Tally details are incomplete")
    if len(chosen) < len(selected):
        parts.append(f"{len(selected) - len(chosen)} are no longer in Tally")
    return _redirect(message="; ".join(parts) + ".")


@router.post("/sync-settings")
def save_sync_settings(
    request: Request,
    sync_minutes: str = Form(...),
    db: Session = Depends(get_db),
):
    require_user(request, db, roles=ADMIN_ROLES)
    try:
        minutes = save_sync_interval(db, sync_minutes)
    except ValueError as exc:
        return _redirect(error=str(exc))
    if minutes == 0:
        return _redirect(message="Live sync is off.")
    return _redirect(message=f"Live sync runs every {minutes} minutes.")


@router.post("/sync-now")
def sync_now(request: Request, db: Session = Depends(get_db)):
    require_user(request, db, roles=ADMIN_ROLES)
    try:
        summary = run_tally_product_sync()
    except TallyDataError as exc:
        return _redirect(error=f"Live sync failed: {exc}")
    return _redirect(
        message=(
            f"Live sync checked {summary.checked} products, updated {summary.updated}, "
            f"and could not find {summary.missing} in Tally."
        )
    )
