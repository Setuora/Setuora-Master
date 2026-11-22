"""Read-only sync status snapshot for the desktop Sync Monitor.

Master writes a small JSON file every few seconds with what has moved between
Lite, Master and Tally and what is still waiting. The data folder is limited to
Administrators, so the monitor reads this file instead of the database. It holds
counts and voucher references only, never credentials or settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    Batch,
    BatchStatus,
    FranchiseNode,
    InboundEvent,
    NetworkStock,
    NodeCommand,
    Serial,
    TallyReceipt,
    utc_now,
)
from app.services.network_ingest import TALLY_OPENING_BATCH
from app.services.settings import get_setting, is_tally_enabled
from app.services.tally_receipts import SYNC_LAST_AT_KEY, SYNC_LAST_RESULT_KEY

logger = logging.getLogger("setuora")

WORKER_STATE_KEY = "setuora_sync_status_worker_task"
INTERVAL_SECONDS = 15
STATUS_FILE_NAME = "sync-status.json"
LIST_LIMIT = 50
WAITING_STATUSES = (
    BatchStatus.SUBMITTED.value,
    BatchStatus.PENDING_SYNC.value,
    BatchStatus.SYNCING.value,
)
PROBLEM_STATUSES = (BatchStatus.FAILED.value, BatchStatus.REVIEW_REQUIRED.value)


def status_dir() -> Path:
    return Path(os.getenv("SETUORA_STATUS_DIR", "status"))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _batch_row(batch: Batch, franchise: str | None) -> dict:
    return {
        "franchise": franchise,
        "batch": batch.batch_number,
        "type": batch.batch_type,
        "status": batch.status,
        "party": batch.party_name,
        "voucher_type": batch.tally_voucher_type,
        "tally_voucher": batch.tally_voucher_number,
        "retries": batch.retry_count,
        "error": (batch.last_error or "")[:300] or None,
        "created_at": _iso(batch.created_at),
        "synced_at": _iso(batch.synced_at),
    }


def _franchise_snapshot(db: Session, node: FranchiseNode, now: datetime) -> dict:
    offline_after = timedelta(minutes=max(1, get_settings().franchise_offline_minutes))
    last_seen = node.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)

    commands = db.execute(
        select(
            NodeCommand.command_type,
            NodeCommand.acknowledged_at.is_(None),
            func.count(NodeCommand.id),
            func.min(NodeCommand.created_at),
        )
        .where(NodeCommand.target_franchise_id == node.id)
        .group_by(NodeCommand.command_type, NodeCommand.acknowledged_at.is_(None))
    ).all()
    to_lite: dict[str, dict] = {}
    for command_type, waiting, count, oldest in commands:
        entry = to_lite.setdefault(command_type, {"waiting": 0, "delivered": 0, "oldest_waiting": None})
        if waiting:
            entry["waiting"] = count
            entry["oldest_waiting"] = _iso(oldest)
        else:
            entry["delivered"] = count

    stock_rows = db.execute(
        select(NetworkStock.status, Serial.product_batch_number == TALLY_OPENING_BATCH, func.count())
        .join(Serial, NetworkStock.serial_id == Serial.id)
        .where(NetworkStock.current_franchise_id == node.id)
        .group_by(NetworkStock.status, Serial.product_batch_number == TALLY_OPENING_BATCH)
    ).all()
    stock: dict[str, int] = {}
    for status, opening, count in stock_rows:
        key = "AWAITING_STOCK_IN" if status == "GENERATED" and opening else status
        stock[key] = stock.get(key, 0) + count

    events = db.execute(
        select(InboundEvent.event_type, func.count(InboundEvent.id), func.max(InboundEvent.received_at))
        .where(InboundEvent.franchise_id == node.id)
        .group_by(InboundEvent.event_type)
    ).all()
    from_lite = {etype: {"count": count, "last": _iso(last)} for etype, count, last in events}

    tally_counts = dict(
        db.execute(
            select(Batch.status, func.count(Batch.id))
            .join(InboundEvent, InboundEvent.tally_batch_id == Batch.id)
            .where(InboundEvent.franchise_id == node.id)
            .group_by(Batch.status)
        ).all()
    )
    last_synced = db.scalar(
        select(func.max(Batch.synced_at))
        .join(InboundEvent, InboundEvent.tally_batch_id == Batch.id)
        .where(InboundEvent.franchise_id == node.id)
    )

    receipts = db.execute(
        select(
            func.count(TallyReceipt.id),
            func.coalesce(func.sum(TallyReceipt.total_amount), 0),
            func.max(TallyReceipt.voucher_date),
            func.max(TallyReceipt.last_synced_at),
        ).where(TallyReceipt.franchise_id == node.id)
    ).one()

    return {
        "code": node.code,
        "name": node.name,
        "godown": node.tally_godown_name,
        "voucher_types": {
            "sale": node.tally_sales_voucher_type,
            "sales_return": node.tally_sales_return_voucher_type,
            "receipt": node.tally_receipt_voucher_type,
        },
        "online": bool(node.active and last_seen and last_seen >= now - offline_after),
        "last_seen_at": _iso(last_seen),
        "last_sequence": node.last_sequence,
        "to_lite": to_lite,
        "from_lite": from_lite,
        "stock": stock,
        "to_tally": {"by_status": tally_counts, "last_synced_at": _iso(last_synced)},
        "from_tally": {
            "receipts": receipts[0],
            "receipts_total": float(receipts[1] or 0),
            "last_receipt_date": receipts[2].isoformat() if receipts[2] else None,
            "last_receipt_sync": _iso(receipts[3]),
        },
    }


def build_snapshot(db: Session) -> dict:
    now = utc_now()
    nodes = db.scalars(select(FranchiseNode).order_by(FranchiseNode.code)).all()
    codes = dict(
        db.execute(select(InboundEvent.tally_batch_id, FranchiseNode.code).join(FranchiseNode)).all()
    )
    open_batches = db.scalars(
        select(Batch)
        .where(Batch.status.in_(WAITING_STATUSES + PROBLEM_STATUSES))
        .order_by(Batch.created_at)
        .limit(LIST_LIMIT)
    ).all()
    recent = db.scalars(
        select(Batch)
        .where(Batch.status == BatchStatus.SYNCED.value, Batch.synced_at.is_not(None))
        .order_by(Batch.synced_at.desc())
        .limit(20)
    ).all()
    return {
        "generated_at": _iso(now),
        "refresh_seconds": INTERVAL_SECONDS,
        "tally_enabled": is_tally_enabled(db),
        "receipt_sync": {
            "last_at": get_setting(db, SYNC_LAST_AT_KEY, "") or None,
            "last_result": get_setting(db, SYNC_LAST_RESULT_KEY, "") or None,
        },
        "franchises": [_franchise_snapshot(db, node, now) for node in nodes if node.active],
        "tally_waiting": [_batch_row(b, codes.get(b.id)) for b in open_batches],
        "tally_recent": [_batch_row(b, codes.get(b.id)) for b in recent],
    }


def write_snapshot() -> Path:
    with SessionLocal() as db:
        snapshot = build_snapshot(db)
    folder = status_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / STATUS_FILE_NAME
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, indent=1, default=str), encoding="utf-8")
    os.replace(temporary, target)
    return target


async def sync_status_worker_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(write_snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Sync status snapshot failed")
        await asyncio.sleep(INTERVAL_SECONDS)


def start_sync_status_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task and not task.done():
        return
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(sync_status_worker_loop()))


async def stop_sync_status_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if not task:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    setattr(app.state, WORKER_STATE_KEY, None)
