from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_db
from app.middleware import (
    CSRFOriginMiddleware,
    NodeAPIBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionActivityMiddleware,
)
from app.routers import (
    account,
    auth,
    maintenance,
    master_console,
    node_api,
    receipts,
    tally_check,
    users,
)
from app.routers import settings as settings_router
from app.services.backup_worker import start_backup_worker, stop_backup_worker
from app.services.bootstrap import bootstrap
from app.services.schema import ensure_runtime_schema
from app.services.sync_worker import start_retry_worker, stop_retry_worker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.using_default_secret:
        raise RuntimeError(
            "APP_SECRET_KEY is insecure. Set it to a long random string before startup."
        )
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema()
    with SessionLocal() as db:
        bootstrap(db)
    start_retry_worker(app)
    start_backup_worker(app)
    try:
        yield
    finally:
        await stop_retry_worker(app)
        await stop_backup_worker(app)


def create_app(app_mode: str | None = None) -> FastAPI:
    settings = get_settings()
    selected_mode = (app_mode or settings.app_mode).strip().lower()
    if selected_mode != "master":
        raise RuntimeError("Setuora-Master only supports master mode.")
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(SessionActivityMiddleware)
    app.add_middleware(CSRFOriginMiddleware)
    app.add_middleware(NodeAPIBodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(master_console.router)
    app.include_router(node_api.router)
    app.include_router(receipts.router)
    app.include_router(settings_router.router)
    app.include_router(tally_check.router)
    app.include_router(maintenance.router)
    app.include_router(users.router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse("app/static/favicon.svg", media_type="image/svg+xml")

    @app.get("/health")
    def health(db: Annotated[Session, Depends(get_db)]) -> dict[str, str]:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "role": selected_mode}

    return app


app = create_app()
