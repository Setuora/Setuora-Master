"""Master-only QR creation, delivery status, and printable labels."""

from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from qrcode.image.svg import SvgPathImage
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import ADMIN_ROLES, require_user
from app.database import get_db
from app.models import FranchiseNode, NetworkStock, NodeCommand, Serial, WarehouseLevel
from app.services.qr_allocation import allocate_qr_serials, get_or_create_franchise_product
from app.templates import templates

router = APIRouter(prefix="/franchises", tags=["master-qr"])


def _node(db: Session, public_id: str) -> FranchiseNode:
    node = db.scalar(select(FranchiseNode).where(FranchiseNode.public_id == public_id))
    if node is None:
        raise HTTPException(status_code=404, detail="Franchise not found")
    return node


def _command(db: Session, node: FranchiseNode, command_id: str) -> NodeCommand:
    command = db.scalar(
        select(NodeCommand).where(
            NodeCommand.public_id == command_id,
            NodeCommand.target_franchise_id == node.id,
            NodeCommand.command_type == "QR_ALLOCATED",
        )
    )
    if command is None:
        raise HTTPException(status_code=404, detail="QR allocation not found")
    return command


def _recent_commands(db: Session, node: FranchiseNode) -> list[dict]:
    commands = db.scalars(
        select(NodeCommand)
        .where(
            NodeCommand.target_franchise_id == node.id,
            NodeCommand.command_type == "QR_ALLOCATED",
        )
        .order_by(desc(NodeCommand.id))
        .limit(100)
    ).all()
    return [{"command": command, "payload": json.loads(command.payload_json)} for command in commands]


def _render(request: Request, db: Session, user, node: FranchiseNode, *, error=None, values=None):
    return templates.TemplateResponse(
        request,
        "master/qr_allocation.html",
        {
            "request": request,
            "user": user,
            "node": node,
            "error": error,
            "values": values or {},
            "request_id": str(uuid4()),
            "warehouse_levels": [level.value for level in WarehouseLevel],
            "commands": _recent_commands(db, node),
        },
        status_code=400 if error else 200,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{public_id}/qr")
def qr_page(request: Request, public_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db, roles=ADMIN_ROLES)
    return _render(request, db, user, _node(db, public_id))


@router.post("/{public_id}/qr")
def create_qr_allocation(
    request: Request,
    public_id: str,
    request_id: str = Form(...),
    product_code: str = Form(...),
    product_name: str = Form(""),
    tally_stock_item_name: str = Form(""),
    hsn: str = Form(""),
    gst_rate: float = Form(0),
    unit: str = Form("Pcs"),
    default_rate: float = Form(0),
    sales_discount_rate: float = Form(0),
    quantity: int = Form(...),
    product_batch_number: str = Form(""),
    mfg_date: str = Form(""),
    expiry_date: str = Form(""),
    warehouse: str = Form(""),
    warehouse_level: str = Form(WarehouseLevel.COMPANY_WAREHOUSE.value),
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    node = _node(db, public_id)
    values = {
        "product_code": product_code,
        "product_name": product_name,
        "tally_stock_item_name": tally_stock_item_name,
        "hsn": hsn,
        "gst_rate": gst_rate,
        "unit": unit,
        "default_rate": default_rate,
        "sales_discount_rate": sales_discount_rate,
        "quantity": quantity,
        "product_batch_number": product_batch_number,
        "mfg_date": mfg_date,
        "expiry_date": expiry_date,
        "warehouse": warehouse,
        "warehouse_level": warehouse_level,
    }
    try:
        product = get_or_create_franchise_product(
            db,
            node,
            product_code=product_code,
            product_name=product_name,
            tally_stock_item_name=tally_stock_item_name,
            hsn=hsn,
            gst_rate=gst_rate,
            unit=unit,
            default_rate=default_rate,
            sales_discount_rate=sales_discount_rate,
        )
        allocate_qr_serials(
            db,
            node=node,
            product=product,
            product_code=product_code,
            quantity=quantity,
            product_batch_number=product_batch_number or None,
            mfg_date=date.fromisoformat(mfg_date) if mfg_date else None,
            expiry_date=date.fromisoformat(expiry_date) if expiry_date else None,
            warehouse=warehouse or None,
            warehouse_level=warehouse_level,
            allocation_id=request_id,
            created_by_id=user.id,
        )
        db.commit()
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = (
            "QR allocation conflicted with an existing request or serial. Please retry."
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        return _render(request, db, user, node, error=message, values=values)
    return RedirectResponse(f"/franchises/{public_id}/qr", status_code=303)


@router.get("/{public_id}/qr/{command_id}/labels")
def qr_labels(
    request: Request,
    public_id: str,
    command_id: str,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    node = _node(db, public_id)
    command = _command(db, node, command_id)
    payload = json.loads(command.payload_json)
    serials = payload["serials"]
    if page < 1 or (page - 1) * 100 >= len(serials):
        raise HTTPException(status_code=404, detail="Label page not found")
    return templates.TemplateResponse(
        request,
        "master/qr_labels.html",
        {
            "request": request,
            "user": user,
            "node": node,
            "command": command,
            "payload": payload,
            "serials": serials[(page - 1) * 100 : page * 100],
            "page": page,
            "page_count": (len(serials) + 99) // 100,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{public_id}/qr/{serial_number}/image.svg")
def qr_image(request: Request, public_id: str, serial_number: str, db: Session = Depends(get_db)):
    require_user(request, db, roles=ADMIN_ROLES)
    node = _node(db, public_id)
    serial = db.scalar(
        select(Serial)
        .join(NetworkStock, NetworkStock.serial_id == Serial.id)
        .where(
            Serial.serial_number == serial_number,
            NetworkStock.origin_franchise_id == node.id,
        )
    )
    if serial is None:
        raise HTTPException(status_code=404, detail="QR serial not found")
    image = qrcode.make(serial.serial_number, image_factory=SvgPathImage)
    return Response(
        image.to_string(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )
