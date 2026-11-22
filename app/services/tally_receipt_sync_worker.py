from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from fastapi import FastAPI

from app.services.tally_godown_qr import run_godown_auto_sync
from app.services.tally_masters import TallyDataError
from app.services.tally_receipts import run_scheduled_receipt_sync

logger = logging.getLogger("setuora")

WORKER_STATE_KEY = "setuora_tally_receipt_sync_worker_task"
CHECK_INTERVAL_SECONDS = 60


async def tally_receipt_sync_worker_loop() -> None:
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            summaries = await asyncio.to_thread(run_scheduled_receipt_sync)
            for summary in summaries or []:
                if summary.created or summary.updated:
                    logger.info("Tally receipt auto sync %s", summary.describe())
        except asyncio.CancelledError:
            raise
        except TallyDataError as exc:
            logger.warning("Tally receipt auto sync failed: %s", exc)
        except Exception:
            logger.exception("Tally receipt auto sync failed")
        try:
            # New stock booked into a franchise's Tally godown becomes its QR stock.
            result = await asyncio.to_thread(run_godown_auto_sync)
            if result:
                logger.info("Tally godown auto sync %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Tally godown auto sync failed")


def start_tally_receipt_sync_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task and not task.done():
        return
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(tally_receipt_sync_worker_loop()))


async def stop_tally_receipt_sync_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if not task:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    setattr(app.state, WORKER_STATE_KEY, None)
