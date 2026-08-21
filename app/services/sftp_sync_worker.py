from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from fastapi import FastAPI

from app.config import get_settings
from app.database import SessionLocal
from app.services.sftp_sync import run_sftp_sync_cycle

logger = logging.getLogger(__name__)
WORKER_STATE_KEY = "sftp_tally_sync_worker"


async def sftp_sync_worker_loop() -> None:
    settings = get_settings()
    while True:
        try:
            await asyncio.to_thread(_run_cycle)
            await asyncio.sleep(settings.sftp_sync_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SFTP Tally sync worker failed")
            await asyncio.sleep(settings.sftp_sync_interval_seconds)


def _run_cycle() -> None:
    with SessionLocal() as db:
        run_sftp_sync_cycle(db)


def start_sftp_sync_worker(app: FastAPI) -> None:
    if not get_settings().sftp_sync_enabled:
        return
    setattr(app.state, WORKER_STATE_KEY, asyncio.create_task(sftp_sync_worker_loop()))


async def stop_sftp_sync_worker(app: FastAPI) -> None:
    task = getattr(app.state, WORKER_STATE_KEY, None)
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
