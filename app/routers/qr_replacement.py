"""Administrative requests for Master-issued QR replacements."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import ADMIN_ROLES, require_user
from app.database import get_db
from app.models import FranchiseNode, NodeCommand, Serial
from app.services.qr_replacement_intent import (
    QrReplacementError,
    QrReplacementIntent,
    request_qr_replacement,
)
from app.templates import templates

router = APIRouter(prefix="/network/qr-replacements")


def _page(request: Request, db: Session, user, *, error: str | None = None, message: str | None = None):
    franchises = db.scalars(
        select(FranchiseNode).where(FranchiseNode.active.is_(True)).order_by(FranchiseNode.code)
    ).all()
    intents = db.scalars(
        select(QrReplacementIntent).order_by(QrReplacementIntent.created_at.desc()).limit(50)
    ).all()
    rows = [
        {
            "intent": intent,
            "franchise": db.get(FranchiseNode, intent.franchise_id),
            "old_serial": db.get(Serial, intent.old_serial_id),
            "command": db.get(NodeCommand, intent.command_id),
        }
        for intent in intents
    ]
    return templates.TemplateResponse(
        request,
        "master/qr_replacement.html",
        {
            "request": request,
            "user": user,
            "franchises": franchises,
            "rows": rows,
            "error": error,
            "message": message,
        },
    )


@router.get("")
def replacement_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, roles=ADMIN_ROLES)
    return _page(request, db, user)


@router.post("")
def create_replacement(
    request: Request,
    franchise_id: int = Form(...),
    old_serial_number: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db, roles=ADMIN_ROLES)
    franchise = db.get(FranchiseNode, franchise_id)
    if franchise is None:
        return _page(request, db, user, error="Choose a registered franchise.")
    try:
        intent, command = request_qr_replacement(
            db,
            franchise=franchise,
            old_serial_number=old_serial_number,
            requested_by=user.username,
            reason=reason,
        )
        db.commit()
    except (QrReplacementError, IntegrityError) as exc:
        db.rollback()
        message = (
            "A replacement is already pending for this QR; refresh and try again."
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        return _page(request, db, user, error=message)
    return _page(
        request,
        db,
        user,
        message=(
            f"Replacement {intent.new_serial_number} was reserved for {franchise.code}. "
            f"Lite will receive command {command.public_id} on its next sync."
        ),
    )
