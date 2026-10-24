from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from fastapi import FastAPI

from app.database import SessionLocal
from app.services.tally_masters import TallyDataError
from app.services.tally_products import run_tally_product_sync, sync_is_due

logger = logging.getLogger("setuora")

WORKER_STATE_KEY = "setuora_tally_product_sync_worker_task"
CHECK_INTERVAL_SECONDS = 60


def run_tally_product_sync_if_due() -> bool:
    with SessionLocal() as db:
        if not sync_is_due(db):
            return False
    summary = run_tally_product_sync()
    if summary.updated or summary.missing:
        logger.info(
            "Tally product live sync updated %s and could not find %s of %s products",
            summary.updated,
            summary.missing,
            summary.checked,
        )
    return True


async def tally_product_sync_worker_loop() -> None:
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(run_tally_product_sync_if_due)
        except asyncio.CancelledError:
            raise
        except TallyDataError as exc:
            logger.warning("Tally product live sync failed: %s", exc)
        except Exception:
            logger.exception("Tally product live sync failed")


def start_tally_product_sync_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task and not task.done():
        return
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(tally_product_sync_worker_loop()))


async def stop_tally_product_sync_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if not task:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    setattr(app.state, WORKER_STATE_KEY, None)
