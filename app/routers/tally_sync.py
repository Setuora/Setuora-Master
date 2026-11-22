"""Manual and scheduled two-way sync between central Tally and franchises."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import ADMIN_ROLES, require_user
from app.database import get_db
from app.models import Batch, BatchStatus, FranchiseNode, InboundEvent, TallyReceipt
from app.services import tally_godown_qr as godown_qr
from app.services.settings import get_setting, is_tally_enabled
from app.services.sync_worker import retry_pending_batches
from app.services.tally import TALLY_XML_SUPPORTED_BATCH_TYPES
from app.services.tally_masters import TallyDataError
from app.services.tally_receipts import (
    MAX_SYNC_DAYS,
    MAX_SYNC_MINUTES,
    SYNC_LAST_RESULT_KEY,
    last_sync_at,
    local_today,
    receipt_franchises,
    save_sync_schedule,
    sync_interval_minutes,
    sync_lookback_days,
    sync_receipts,
)
from app.templates import templates

router = APIRouter(prefix="/tally-sync", tags=["master-tally-sync"])

# One manual push sends at most this many vouchers so the page stays responsive.
MANUAL_PUSH_LIMIT = 100
OPEN_STATUSES = (
    BatchStatus.PENDING_SYNC.value,
    BatchStatus.SYNCING.value,
    BatchStatus.FAILED.value,
    BatchStatus.REVIEW_REQUIRED.value,
)


def _redirect(**params: object) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value not in ("", None)})
    return RedirectResponse(f"/tally-sync?{query}" if query else "/tally-sync", status_code=303)


def _parse_date(raw: str, label: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Enter a valid {label}.") from exc


def _open_vouchers_by_franchise(db: Session) -> dict[int, dict[str, int]]:
    rows = db.execute(
        select(InboundEvent.franchise_id, Batch.status, func.count(Batch.id))
        .join(Batch, InboundEvent.tally_batch_id == Batch.id)
        .where(
            Batch.status.in_(OPEN_STATUSES),
            Batch.batch_type.in_(TALLY_XML_SUPPORTED_BATCH_TYPES),
        )
        .group_by(InboundEvent.franchise_id, Batch.status)
    ).all()
    counts: dict[int, dict[str, int]] = {}
    for franchise_id, status, count in rows:
        counts.setdefault(franchise_id, {})[status] = count
    return counts


@router.get("")
def tally_sync_page(
    request: Request,
    franchise: str = "",
    message: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    nodes = list(
        db.scalars(
            select(FranchiseNode).where(FranchiseNode.active.is_(True)).order_by(FranchiseNode.code)
        )
    )
    receipt_query = (
        select(TallyReceipt)
        .options(selectinload(TallyReceipt.franchise))
        .order_by(TallyReceipt.voucher_date.desc(), TallyReceipt.id.desc())
        .limit(300)
    )
    selected = next((node for node in nodes if node.code == franchise.strip().upper()), None)
    if selected is not None:
        receipt_query = receipt_query.where(TallyReceipt.franchise_id == selected.id)
    today = local_today()
    return templates.TemplateResponse(
        request,
        "master/tally_sync.html",
        {
            "request": request,
            "user": user,
            "nodes": nodes,
            "receipt_nodes": receipt_franchises(db),
            "selected": selected,
            "receipts": db.scalars(receipt_query).all(),
            "open_counts": _open_vouchers_by_franchise(db),
            "tally_enabled": is_tally_enabled(db),
            "message": message,
            "error": error,
            "default_from": (today - timedelta(days=sync_lookback_days(db))).isoformat(),
            "default_to": today.isoformat(),
            "sync_minutes": sync_interval_minutes(db),
            "sync_days": sync_lookback_days(db),
            "max_sync_minutes": MAX_SYNC_MINUTES,
            "max_sync_days": MAX_SYNC_DAYS,
            "last_sync_at": last_sync_at(db),
            "last_sync_result": get_setting(db, SYNC_LAST_RESULT_KEY, ""),
            "godown_minutes": godown_qr.auto_sync_interval_minutes(db),
            "max_godown_minutes": godown_qr.MAX_AUTO_SYNC_MINUTES,
            "godown_last_at": godown_qr.auto_sync_last_at(db),
            "godown_last_result": get_setting(db, godown_qr.AUTO_SYNC_LAST_RESULT_KEY, ""),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/receipts")
async def sync_receipts_now(
    request: Request,
    franchise: str = Form("all"),
    from_date: str = Form(...),
    to_date: str = Form(...),
    db: Session = Depends(get_db),
):
    require_user(request, db, roles=ADMIN_ROLES)
    try:
        start = _parse_date(from_date, "start date")
        end = _parse_date(to_date, "end date")
        if (end - start).days > MAX_SYNC_DAYS:
            raise ValueError(f"Sync at most {MAX_SYNC_DAYS} days at a time.")
        franchise_ids = None
        if franchise != "all":
            node = db.scalar(select(FranchiseNode).where(FranchiseNode.public_id == franchise))
            if node is None:
                raise ValueError("Choose a franchise.")
            franchise_ids = [node.id]
        summaries = await asyncio.to_thread(sync_receipts, franchise_ids, start, end)
    except (ValueError, TallyDataError) as exc:
        return _redirect(error=str(exc))
    return _redirect(message=" ".join(summary.describe() for summary in summaries))


@router.post("/push")
async def push_vouchers_now(request: Request, db: Session = Depends(get_db)):
    require_user(request, db, roles=ADMIN_ROLES)
    if not is_tally_enabled(db):
        return _redirect(error="Turn on Tally sync in Settings before sending vouchers.")
    processed = await asyncio.to_thread(retry_pending_batches, MANUAL_PUSH_LIMIT)
    if processed == 0:
        return _redirect(message="No vouchers were waiting for Tally.")
    return _redirect(
        message=(
            f"Sent {processed} voucher{'' if processed == 1 else 's'} to Tally. "
            "Any that Tally rejected are listed in the Tally queue with the reason."
        )
    )


@router.post("/schedule")
def save_schedule(
    request: Request,
    sync_minutes: str = Form(...),
    sync_days: str = Form(...),
    godown_minutes: str = Form(""),
    db: Session = Depends(get_db),
):
    require_user(request, db, roles=ADMIN_ROLES)
    try:
        save_sync_schedule(db, sync_minutes, sync_days)
        if godown_minutes.strip():
            godown_qr.save_auto_sync_interval(db, godown_minutes)
    except ValueError as exc:
        return _redirect(error=str(exc))
    return _redirect(message="Auto sync schedule saved.")
