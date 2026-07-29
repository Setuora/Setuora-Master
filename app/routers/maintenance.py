import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import require_permission, require_user
from app.config import get_settings
from app.database import get_db
from app.models import Role
from app.services.backup import (
    backup_status,
    create_sqlite_backup,
    sqlite_database_path,
    update_backup_settings,
)
from app.services.backup_worker import start_backup_worker, stop_backup_worker
from app.templates import templates

router = APIRouter(prefix="/maintenance")
logger = logging.getLogger("setuora")


@router.get("")
def maintenance_page(
    request: Request,
    error: str = "",
    success: str = "",
    db: Session = Depends(get_db),
):
    user = require_permission(request, db, "backup_data")
    error_message = {
        "backup_failed": ("Backup could not be created. Check the database file and try again."),
    }.get(error, error)
    success_message = {
        "backup_settings": "Backup settings saved.",
    }.get(success, success)
    return templates.TemplateResponse(
        request,
        "maintenance.html",
        {
            "request": request,
            "user": user,
            "database_path": sqlite_database_path(),
            "backup_status": backup_status(),
            "backup_config": get_settings(),
            "error": error_message or None,
            "success": success_message or None,
        },
    )


@router.post("/backup-settings")
async def save_backup_settings(
    request: Request,
    automatic_backups_enabled: str = Form("false"),
    backup_directory: str = Form(...),
    backup_interval_hours: str = Form(...),
    backup_retention_count: str = Form(...),
    backup_offsite_directory: str = Form(""),
    db: Session = Depends(get_db),
):
    require_user(request, db, {Role.SUPER_ADMIN})
    try:
        status = update_backup_settings(
            enabled=automatic_backups_enabled == "true",
            backup_directory=backup_directory,
            interval_hours=backup_interval_hours,
            retention_count=backup_retention_count,
            offsite_directory=backup_offsite_directory,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/maintenance?error={quote(str(exc))}",
            status_code=303,
        )

    await stop_backup_worker(request.app)
    if status.enabled:
        start_backup_worker(request.app)
    return RedirectResponse("/maintenance?success=backup_settings", status_code=303)


@router.get("/backup.db")
def download_backup(request: Request, db: Session = Depends(get_db)):
    require_permission(request, db, "backup_download")
    try:
        backup = create_sqlite_backup()
    except Exception:
        logger.exception("Backup download failed")
        return RedirectResponse("/maintenance?error=backup_failed", status_code=303)
    return Response(
        backup.data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={backup.filename}"},
    )
