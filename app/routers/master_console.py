from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from io import StringIO

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth import ADMIN_ROLES, require_user
from app.config import get_settings
from app.database import get_db
from app.models import (
    Batch,
    BatchItem,
    BatchStatus,
    FranchiseNode,
    InboundEvent,
    NetworkStock,
    NodeCredential,
    Product,
    Role,
    Serial,
    StockTransfer,
    StockTransferItem,
    TransferStatus,
    utc_now,
)
from app.services.node_auth import (
    create_franchise_node,
    provision_node_credential,
    rotate_node_credential,
)
from app.services.tally import sync_batch
from app.templates import templates

router = APIRouter()
FINANCIAL_EVENT_TYPES = {
    "PURCHASE",
    "RECEIVE",
    "SALE",
    "SALES_RETURN",
    "PURCHASE_RETURN",
    "ISSUE",
}
TALLY_EVENT_TYPES = {"PURCHASE", "RECEIVE", "SALE", "SALES_RETURN"}
OPEN_TRANSFER_STATUSES = {
    TransferStatus.DRAFT.value,
    TransferStatus.DISPATCHED.value,
    TransferStatus.PARTIALLY_RECEIVED.value,
}
MASTER_MONITOR_ROLES = {Role.DIRECTORS, Role.ADMIN, Role.SUPER_ADMIN}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_online(node: FranchiseNode, now: datetime | None = None) -> bool:
    last_seen = _as_utc(node.last_seen_at)
    if not node.active or last_seen is None:
        return False
    cutoff = (now or utc_now()) - timedelta(
        minutes=max(1, get_settings().franchise_offline_minutes)
    )
    return last_seen >= cutoff


def _parse_datetime(value: str, field_name: str, *, end: bool = False) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}",
        ) from exc
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    if end and len(raw) == 10:
        parsed += timedelta(days=1)
    return parsed


def _franchise_options(db: Session) -> list[FranchiseNode]:
    return db.scalars(select(FranchiseNode).order_by(FranchiseNode.code)).all()


def _event_query(
    *,
    franchise_code: str = "",
    event_type: str = "",
    query: str = "",
    start: str = "",
    end: str = "",
):
    statement = (
        select(InboundEvent)
        .join(FranchiseNode, InboundEvent.franchise_id == FranchiseNode.id)
        .order_by(desc(InboundEvent.occurred_at), desc(InboundEvent.id))
        .options(
            selectinload(InboundEvent.franchise),
            selectinload(InboundEvent.tally_batch),
        )
    )
    conditions = []
    if franchise_code.strip():
        conditions.append(FranchiseNode.code == franchise_code.strip().upper())
    if event_type.strip():
        conditions.append(InboundEvent.event_type == event_type.strip().upper())
    if query.strip():
        like = f"%{query.strip()}%"
        conditions.append(
            or_(
                InboundEvent.event_id.ilike(like),
                InboundEvent.reference.ilike(like),
                InboundEvent.actor.ilike(like),
                InboundEvent.payload_json.ilike(like),
            )
        )
    start_at = _parse_datetime(start, "start date")
    end_at = _parse_datetime(end, "end date", end=True)
    if start_at:
        conditions.append(InboundEvent.occurred_at >= start_at)
    if end_at:
        conditions.append(InboundEvent.occurred_at < end_at)
    if conditions:
        statement = statement.where(and_(*conditions))
    return statement


@router.get("/")
def master_dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, roles=MASTER_MONITOR_ROLES)
    nodes = _franchise_options(db)
    now = utc_now()
    online_count = sum(1 for node in nodes if _is_online(node, now))
    stock_by_node = db.execute(
        select(
            FranchiseNode,
            func.count(NetworkStock.id),
        )
        .outerjoin(
            NetworkStock,
            and_(
                NetworkStock.current_franchise_id == FranchiseNode.id,
                NetworkStock.status == "IN_STOCK",
            ),
        )
        .group_by(FranchiseNode.id)
        .order_by(FranchiseNode.code)
    ).all()
    counts = {
        "franchises": sum(1 for node in nodes if node.active),
        "online": online_count,
        "in_stock": db.scalar(
            select(func.count(NetworkStock.id)).where(NetworkStock.status == "IN_STOCK")
        )
        or 0,
        "in_transit": db.scalar(
            select(func.count(NetworkStock.id)).where(NetworkStock.status == "IN_TRANSIT")
        )
        or 0,
        "pending_tally": db.scalar(
            select(func.count(Batch.id)).where(
                Batch.status.in_(
                    {
                        BatchStatus.SUBMITTED.value,
                        BatchStatus.PENDING_SYNC.value,
                        BatchStatus.SYNCING.value,
                    }
                )
            )
        )
        or 0,
        "failed_tally": db.scalar(
            select(func.count(Batch.id)).where(Batch.status == BatchStatus.FAILED.value)
        )
        or 0,
        "open_transfers": db.scalar(
            select(func.count(StockTransfer.id)).where(
                StockTransfer.status.in_(OPEN_TRANSFER_STATUSES)
            )
        )
        or 0,
    }
    recent_events = db.scalars(_event_query().limit(12)).all()
    recent_transfers = db.scalars(
        select(StockTransfer)
        .order_by(desc(StockTransfer.updated_at), desc(StockTransfer.id))
        .limit(8)
        .options(
            selectinload(StockTransfer.source_franchise),
            selectinload(StockTransfer.destination_franchise),
            selectinload(StockTransfer.items),
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "master/dashboard.html",
        {
            "request": request,
            "user": user,
            "counts": counts,
            "nodes": nodes,
            "online_by_id": {node.id: _is_online(node, now) for node in nodes},
            "stock_by_node": stock_by_node,
            "recent_events": recent_events,
            "recent_transfers": recent_transfers,
        },
    )


@router.get("/franchises")
def franchises_page(
    request: Request,
    created: str = "",
    rotated: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    nodes = db.scalars(
        select(FranchiseNode)
        .order_by(FranchiseNode.code)
        .options(selectinload(FranchiseNode.credentials))
    ).all()
    return templates.TemplateResponse(
        request,
        "master/franchises.html",
        {
            "request": request,
            "user": user,
            "nodes": nodes,
            "online_by_id": {node.id: _is_online(node) for node in nodes},
            "created": created,
            "rotated": rotated,
            "api_key": None,
            "error": None,
        },
    )


def _render_franchises_with_result(
    request: Request,
    db: Session,
    user,
    *,
    api_key: str | None = None,
    error: str | None = None,
):
    nodes = db.scalars(
        select(FranchiseNode)
        .order_by(FranchiseNode.code)
        .options(selectinload(FranchiseNode.credentials))
    ).all()
    return templates.TemplateResponse(
        request,
        "master/franchises.html",
        {
            "request": request,
            "user": user,
            "nodes": nodes,
            "online_by_id": {node.id: _is_online(node) for node in nodes},
            "created": "",
            "rotated": "",
            "api_key": api_key,
            "error": error,
        },
        status_code=400 if error else 200,
    )


@router.post("/franchises")
def create_franchise(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    location: str = Form(...),
    tally_godown_name: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    try:
        if not code.strip() or not name.strip() or not location.strip():
            raise ValueError("Code, name, and location are required.")
        node = create_franchise_node(
            db,
            code=code,
            name=name,
            location=location,
            tally_godown_name=tally_godown_name or None,
            commit=False,
        )
        provisioned = provision_node_credential(db, node, commit=False)
        db.commit()
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = (
            "Franchise code and name must each be unique."
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        return _render_franchises_with_result(request, db, user, error=message)
    return _render_franchises_with_result(
        request,
        db,
        user,
        api_key=provisioned.api_key,
    )


@router.post("/franchises/{public_id}/credentials")
def rotate_franchise_credential(
    request: Request,
    public_id: str,
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    node = db.scalar(select(FranchiseNode).where(FranchiseNode.public_id == public_id))
    if node is None:
        raise HTTPException(status_code=404, detail="Franchise not found")
    try:
        provisioned = rotate_node_credential(db, node)
    except ValueError as exc:
        db.rollback()
        return _render_franchises_with_result(request, db, user, error=str(exc))
    return _render_franchises_with_result(
        request,
        db,
        user,
        api_key=provisioned.api_key,
    )


@router.post("/franchises/{public_id}/status")
def set_franchise_status(
    request: Request,
    public_id: str,
    active: str = Form(...),
    db: Session = Depends(get_db),
):
    require_user(request, db, roles=ADMIN_ROLES)
    node = db.scalar(select(FranchiseNode).where(FranchiseNode.public_id == public_id))
    if node is None:
        raise HTTPException(status_code=404, detail="Franchise not found")
    node.active = active.strip().lower() == "true"
    if not node.active:
        now = utc_now()
        for credential in db.scalars(
            select(NodeCredential).where(
                NodeCredential.franchise_id == node.id,
                NodeCredential.revoked_at.is_(None),
            )
        ).all():
            credential.revoked_at = now
    db.commit()
    return RedirectResponse("/franchises", status_code=303)


@router.get("/network/events")
def network_events(
    request: Request,
    franchise: str = "",
    event_type: str = "",
    q: str = "",
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    events = db.scalars(
        _event_query(
            franchise_code=franchise,
            event_type=event_type,
            query=q,
            start=start,
            end=end,
        ).limit(500)
    ).all()
    event_types = db.scalars(
        select(InboundEvent.event_type).distinct().order_by(InboundEvent.event_type)
    ).all()
    return templates.TemplateResponse(
        request,
        "master/events.html",
        {
            "request": request,
            "user": user,
            "events": events,
            "franchises": _franchise_options(db),
            "event_types": event_types,
            "filters": {
                "franchise": franchise,
                "event_type": event_type,
                "q": q,
                "start": start,
                "end": end,
            },
        },
    )


@router.get("/network/events/{event_id}")
def network_event_detail(
    request: Request,
    event_id: str,
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    event = db.scalar(
        select(InboundEvent)
        .where(InboundEvent.event_id == event_id)
        .options(
            selectinload(InboundEvent.franchise),
            selectinload(InboundEvent.tally_batch),
        )
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    try:
        payload = json.dumps(json.loads(event.payload_json), indent=2, sort_keys=True)
        result = json.dumps(json.loads(event.result_json), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        payload = event.payload_json
        result = event.result_json
    return templates.TemplateResponse(
        request,
        "master/event_detail.html",
        {
            "request": request,
            "user": user,
            "event": event,
            "payload": payload,
            "result": result,
        },
    )


@router.get("/network/transfers")
def network_transfers(
    request: Request,
    franchise: str = "",
    transfer_status: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=MASTER_MONITOR_ROLES)
    statement = (
        select(StockTransfer)
        .order_by(desc(StockTransfer.updated_at), desc(StockTransfer.id))
        .options(
            selectinload(StockTransfer.source_franchise),
            selectinload(StockTransfer.destination_franchise),
            selectinload(StockTransfer.items),
        )
    )
    conditions = []
    if transfer_status.strip():
        conditions.append(StockTransfer.status == transfer_status.strip().upper())
    if franchise.strip():
        code = franchise.strip().upper()
        source_ids = select(FranchiseNode.id).where(FranchiseNode.code == code)
        conditions.append(
            or_(
                StockTransfer.source_franchise_id.in_(source_ids),
                StockTransfer.destination_franchise_id.in_(source_ids),
            )
        )
    if conditions:
        statement = statement.where(and_(*conditions))
    transfers = db.scalars(statement.limit(500)).all()
    return templates.TemplateResponse(
        request,
        "master/transfers.html",
        {
            "request": request,
            "user": user,
            "transfers": transfers,
            "franchises": _franchise_options(db),
            "statuses": [value.value for value in TransferStatus],
            "filters": {"franchise": franchise, "status": transfer_status},
        },
    )


@router.get("/network/transfers/{public_id}")
def network_transfer_detail(
    request: Request,
    public_id: str,
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=MASTER_MONITOR_ROLES)
    transfer = db.scalar(
        select(StockTransfer)
        .where(StockTransfer.public_id == public_id)
        .options(
            selectinload(StockTransfer.source_franchise),
            selectinload(StockTransfer.destination_franchise),
            selectinload(StockTransfer.items)
            .selectinload(StockTransferItem.serial)
            .selectinload(Serial.product),
        )
    )
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return templates.TemplateResponse(
        request,
        "master/transfer_detail.html",
        {"request": request, "user": user, "transfer": transfer},
    )


def _movement_rows(
    db: Session,
    *,
    franchise_code: str = "",
    start: str = "",
    end: str = "",
) -> list[dict[str, object]]:
    start_at = _parse_datetime(start, "start date")
    end_at = _parse_datetime(end, "end date", end=True)
    node_by_id = {node.id: node for node in _franchise_options(db)}
    selected_ids = set(node_by_id)
    if franchise_code.strip():
        selected_ids = {
            node_id
            for node_id, node in node_by_id.items()
            if node.code == franchise_code.strip().upper()
        }

    totals: dict[tuple[int, str], dict[str, object]] = {}

    def row_for(node_id: int, product: Product) -> dict[str, object]:
        key = (node_id, product.product_code)
        return totals.setdefault(
            key,
            {
                "franchise": node_by_id[node_id],
                "product_code": product.product_code,
                "product_name": product.product_name,
                "current_stock": 0,
                "purchased": 0,
                "sold": 0,
                "sales_returns": 0,
                "purchase_returns": 0,
                "issued": 0,
                "transferred_out": 0,
                "transferred_in": 0,
            },
        )

    stock_statement = (
        select(NetworkStock.current_franchise_id, Product, func.count(NetworkStock.id))
        .join(Serial, NetworkStock.serial_id == Serial.id)
        .join(Product, Serial.product_id == Product.id)
        .where(NetworkStock.status == "IN_STOCK")
        .group_by(NetworkStock.current_franchise_id, Product.id)
    )
    if selected_ids:
        stock_statement = stock_statement.where(NetworkStock.current_franchise_id.in_(selected_ids))
    else:
        return []
    for node_id, product, quantity in db.execute(stock_statement):
        row_for(node_id, product)["current_stock"] = int(quantity or 0)

    movement_statement = (
        select(
            InboundEvent.franchise_id,
            InboundEvent.event_type,
            Product,
            func.sum(BatchItem.quantity),
        )
        .join(Batch, InboundEvent.tally_batch_id == Batch.id)
        .join(BatchItem, BatchItem.batch_id == Batch.id)
        .join(Serial, BatchItem.serial_id == Serial.id)
        .join(Product, Serial.product_id == Product.id)
        .where(
            InboundEvent.franchise_id.in_(selected_ids),
            InboundEvent.event_type.in_(FINANCIAL_EVENT_TYPES),
        )
        .group_by(InboundEvent.franchise_id, InboundEvent.event_type, Product.id)
    )
    if start_at:
        movement_statement = movement_statement.where(InboundEvent.occurred_at >= start_at)
    if end_at:
        movement_statement = movement_statement.where(InboundEvent.occurred_at < end_at)
    field_by_type = {
        "PURCHASE": "purchased",
        "RECEIVE": "purchased",
        "SALE": "sold",
        "SALES_RETURN": "sales_returns",
        "PURCHASE_RETURN": "purchase_returns",
        "ISSUE": "issued",
    }
    for node_id, event_type, product, quantity in db.execute(movement_statement):
        field = field_by_type[event_type]
        row = row_for(node_id, product)
        row[field] = int(row[field]) + int(quantity or 0)

    transfer_out_statement = (
        select(
            StockTransfer.source_franchise_id,
            Product,
            func.sum(StockTransferItem.quantity),
        )
        .join(StockTransferItem, StockTransferItem.transfer_id == StockTransfer.id)
        .join(Serial, StockTransferItem.serial_id == Serial.id)
        .join(Product, Serial.product_id == Product.id)
        .join(InboundEvent, StockTransfer.dispatch_event_id == InboundEvent.id)
        .where(StockTransfer.source_franchise_id.in_(selected_ids))
        .group_by(StockTransfer.source_franchise_id, Product.id)
    )
    if start_at:
        transfer_out_statement = transfer_out_statement.where(InboundEvent.occurred_at >= start_at)
    if end_at:
        transfer_out_statement = transfer_out_statement.where(InboundEvent.occurred_at < end_at)
    for node_id, product, quantity in db.execute(transfer_out_statement):
        row_for(node_id, product)["transferred_out"] = int(quantity or 0)

    transfer_in_statement = (
        select(
            StockTransfer.destination_franchise_id,
            Product,
            func.sum(StockTransferItem.received_quantity),
        )
        .join(StockTransferItem, StockTransferItem.transfer_id == StockTransfer.id)
        .join(Serial, StockTransferItem.serial_id == Serial.id)
        .join(Product, Serial.product_id == Product.id)
        .where(
            StockTransfer.destination_franchise_id.in_(selected_ids),
            StockTransferItem.received_quantity > 0,
        )
        .group_by(StockTransfer.destination_franchise_id, Product.id)
    )
    if start_at:
        transfer_in_statement = transfer_in_statement.where(
            StockTransferItem.received_at >= start_at
        )
    if end_at:
        transfer_in_statement = transfer_in_statement.where(StockTransferItem.received_at < end_at)
    for node_id, product, quantity in db.execute(transfer_in_statement):
        row_for(node_id, product)["transferred_in"] = int(quantity or 0)
    return sorted(
        totals.values(),
        key=lambda item: (
            item["franchise"].code,
            str(item["product_name"]).casefold(),
            item["product_code"],
        ),
    )


@router.get("/network/reports")
def network_reports(
    request: Request,
    franchise: str = "",
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=MASTER_MONITOR_ROLES)
    rows = _movement_rows(
        db,
        franchise_code=franchise,
        start=start,
        end=end,
    )
    summary = {
        key: sum(int(row[key]) for row in rows)
        for key in (
            "current_stock",
            "purchased",
            "sold",
            "sales_returns",
            "purchase_returns",
            "issued",
            "transferred_out",
            "transferred_in",
        )
    }
    return templates.TemplateResponse(
        request,
        "master/reports.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "summary": summary,
            "franchises": _franchise_options(db),
            "filters": {"franchise": franchise, "start": start, "end": end},
        },
    )


@router.get("/network/reports.csv")
def network_reports_csv(
    request: Request,
    franchise: str = "",
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    require_user(request, db, roles=MASTER_MONITOR_ROLES)
    rows = _movement_rows(
        db,
        franchise_code=franchise,
        start=start,
        end=end,
    )
    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        (
            "Franchise",
            "Product Code",
            "Product Name",
            "Current Stock",
            "Purchased",
            "Sold",
            "Sales Returns",
            "Purchase Returns",
            "Issued",
            "Transferred Out",
            "Transferred In",
        )
    )
    for row in rows:
        writer.writerow(
            (
                row["franchise"].code,
                row["product_code"],
                row["product_name"],
                row["current_stock"],
                row["purchased"],
                row["sold"],
                row["sales_returns"],
                row["purchase_returns"],
                row["issued"],
                row["transferred_out"],
                row["transferred_in"],
            )
        )
    return Response(
        stream.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=setuora-network-report.csv"},
    )


@router.get("/network/tally")
def network_tally_queue(
    request: Request,
    tally_status: str = "",
    franchise: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    statement = (
        select(InboundEvent)
        .join(Batch, InboundEvent.tally_batch_id == Batch.id)
        .join(FranchiseNode, InboundEvent.franchise_id == FranchiseNode.id)
        .where(InboundEvent.event_type.in_(TALLY_EVENT_TYPES))
        .order_by(desc(InboundEvent.occurred_at), desc(InboundEvent.id))
        .options(
            selectinload(InboundEvent.franchise),
            selectinload(InboundEvent.tally_batch),
        )
    )
    conditions = []
    if tally_status.strip():
        conditions.append(Batch.status == tally_status.strip().upper())
    if franchise.strip():
        conditions.append(FranchiseNode.code == franchise.strip().upper())
    if conditions:
        statement = statement.where(and_(*conditions))
    events = db.scalars(statement.limit(500)).all()
    return templates.TemplateResponse(
        request,
        "master/tally_queue.html",
        {
            "request": request,
            "user": user,
            "events": events,
            "franchises": _franchise_options(db),
            "statuses": [value.value for value in BatchStatus],
            "filters": {"franchise": franchise, "status": tally_status},
        },
    )


@router.get("/network/tally/{batch_id}")
def network_tally_detail(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    event = db.scalar(
        select(InboundEvent)
        .where(InboundEvent.tally_batch_id == batch_id)
        .options(
            selectinload(InboundEvent.franchise),
            selectinload(InboundEvent.tally_batch).selectinload(Batch.sync_attempts),
        )
    )
    if event is None or event.tally_batch is None:
        raise HTTPException(status_code=404, detail="Tally queue item not found")
    return templates.TemplateResponse(
        request,
        "master/tally_detail.html",
        {
            "request": request,
            "user": user,
            "event": event,
            "batch": event.tally_batch,
        },
    )


@router.post("/network/tally/{batch_id}/retry")
def retry_network_tally_batch(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    require_user(request, db, roles=ADMIN_ROLES)
    event = db.scalar(
        select(InboundEvent)
        .where(InboundEvent.tally_batch_id == batch_id)
        .options(
            selectinload(InboundEvent.tally_batch)
            .selectinload(Batch.items)
            .selectinload(BatchItem.serial)
            .selectinload(Serial.product)
        )
    )
    if event is None or event.tally_batch is None:
        raise HTTPException(status_code=404, detail="Tally queue item not found")
    batch = event.tally_batch
    if batch.status in {
        BatchStatus.SUBMITTED.value,
        BatchStatus.PENDING_SYNC.value,
        BatchStatus.FAILED.value,
    }:
        sync_batch(db, batch)
    return RedirectResponse(f"/network/tally/{batch.id}", status_code=303)
