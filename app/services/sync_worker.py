from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import Batch, BatchItem, BatchStatus, Serial, SyncAttempt, utc_now
from app.services.books_sync import BooksUnavailable, mirror_to_books
from app.services.settings import get_all_settings, is_tally_enabled
from app.services.tally import (
    SYNC_LEASE_MINUTES,
    TALLY_XML_SUPPORTED_BATCH_TYPES,
    find_posted_voucher,
    reconcile_batch,
    sync_batch,
)
from app.services.tally_masters import TallyDataError

WORKER_STATE_KEY = "setuora_retry_worker_task"
logger = logging.getLogger("setuora")


def reconcile_review_batches() -> int:
    """Settle vouchers whose import outcome is unknown by looking for them in Tally.

    A review-required batch halts the Tally queue. Once its last attempt is old
    enough that Tally has finished any import, the voucher is either in Tally
    (synced) or certainly absent (posted again). If Tally cannot answer, the
    batch waits for the next round.
    """
    settled = 0
    with SessionLocal() as db:
        if not is_tally_enabled(db):
            return 0
        settings = get_all_settings(db)
        settle_before = utc_now() - timedelta(minutes=SYNC_LEASE_MINUTES)
        batches = db.scalars(
            select(Batch).where(Batch.status == BatchStatus.REVIEW_REQUIRED.value).order_by(Batch.id)
        ).all()
        for batch in batches:
            last_attempt = db.scalar(
                select(func.max(SyncAttempt.created_at)).where(SyncAttempt.batch_id == batch.id)
            )
            if last_attempt is not None and last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=settle_before.tzinfo)
            if not batch.sync_request_xml or (last_attempt and last_attempt > settle_before):
                continue
            try:
                number = find_posted_voucher(settings, batch.sync_request_xml)
            except TallyDataError as exc:
                logger.info("Tally check for %s postponed: %s", batch.batch_number, exc)
                break
            reconcile_batch(
                db,
                batch,
                imported=number is not None,
                reference=f"Found in Tally as {number}" if number else "",
                actor="automatic Tally check",
            )
            settled += 1
    return settled


def retry_pending_batches(limit: int = 10) -> int:
    reconcile_review_batches()
    with SessionLocal() as db:
        if not is_tally_enabled(db):
            return 0
        batches = db.scalars(
            select(Batch)
            .where(
                or_(
                    Batch.status == BatchStatus.PENDING_SYNC.value,
                    (
                        (Batch.status == BatchStatus.SYNCING.value)
                        & (
                            (Batch.sync_started_at.is_(None))
                            | (
                                Batch.sync_started_at
                                < utc_now() - timedelta(minutes=SYNC_LEASE_MINUTES)
                            )
                        )
                    ),
                ),
                Batch.batch_type.in_(TALLY_XML_SUPPORTED_BATCH_TYPES),
            )
            .order_by(Batch.last_retry_at.is_not(None), Batch.last_retry_at, Batch.created_at)
            .limit(limit)
            .options(
                selectinload(Batch.items)
                .selectinload(BatchItem.serial)
                .selectinload(Serial.product)
            )
        ).all()
        for batch in batches:
            sync_batch(db, batch)
        return len(batches)


async def retry_worker_loop() -> None:
    while True:
        interval = 180
        try:
            with SessionLocal() as db:
                settings = get_all_settings(db)
                try:
                    interval = max(30, int(settings.get("retry_interval_seconds", "180")))
                except ValueError:
                    interval = 180
            await asyncio.sleep(interval)
            await asyncio.to_thread(retry_pending_batches)
            try:
                await asyncio.to_thread(mirror_to_books)
            except BooksUnavailable as exc:
                logger.warning("HNC Books mirror waiting: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending sync retry worker failed")
            await asyncio.sleep(interval)


def start_retry_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task and not task.done():
        return
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(retry_worker_loop()))


async def stop_retry_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if not task:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    setattr(app.state, WORKER_STATE_KEY, None)
