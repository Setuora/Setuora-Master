from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import ADMIN_ROLES, require_user
from app.database import get_db
from app.models import FranchiseNode, Receipt, ReceiptStatus, Role, User, utc_now
from app.services.network_ingest import queue_node_command
from app.templates import templates

router = APIRouter(prefix="/receipts")
RECEIPT_VIEW_ROLES = {Role.SUPER_ADMIN, Role.ADMIN, Role.DIRECTORS}


def _receipt_query():
    return select(Receipt).options(
        selectinload(Receipt.franchise), selectinload(Receipt.reviewed_by)
    )


def _get_receipt(db: Session, public_id: str) -> Receipt:
    receipt = db.scalar(_receipt_query().where(Receipt.public_id == public_id))
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    return receipt


@router.get("")
def receipt_list(
    request: Request,
    receipt_status: str = "",
    franchise: str = "",
    db: Session = Depends(get_db),
):
    user = require_user(request, db, RECEIPT_VIEW_ROLES)
    statement = (
        _receipt_query().join(FranchiseNode).order_by(Receipt.created_at.desc(), Receipt.id.desc())
    )
    normalized_status = receipt_status.strip().upper()
    normalized_franchise = franchise.strip().upper()
    if normalized_status in {status.value for status in ReceiptStatus}:
        statement = statement.where(Receipt.status == normalized_status)
    if normalized_franchise:
        statement = statement.where(FranchiseNode.code == normalized_franchise)
    receipts = db.scalars(statement.limit(500)).all()
    franchises = db.scalars(select(FranchiseNode).order_by(FranchiseNode.code)).all()
    return templates.TemplateResponse(
        request,
        "master/receipts.html",
        {
            "request": request,
            "user": user,
            "receipts": receipts,
            "franchises": franchises,
            "statuses": [status.value for status in ReceiptStatus],
            "filters": {"status": normalized_status, "franchise": normalized_franchise},
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/{public_id}/proof")
def receipt_proof(public_id: str, request: Request, db: Session = Depends(get_db)):
    require_user(request, db, RECEIPT_VIEW_ROLES)
    receipt = _get_receipt(db, public_id)
    return Response(
        receipt.proof_image,
        media_type=receipt.proof_content_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="receipt-{receipt.public_id}.img"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{public_id}/review")
def review_receipt(
    public_id: str,
    request: Request,
    decision: str = Form(...),
    rejection_remarks: str = Form(""),
    db: Session = Depends(get_db),
):
    user: User = require_user(request, db, ADMIN_ROLES)
    receipt = _get_receipt(db, public_id)
    normalized_decision = decision.strip().upper()
    remarks = " ".join(rejection_remarks.split())
    if receipt.status != ReceiptStatus.PENDING.value:
        query = urlencode({"error": "This receipt has already been reviewed."})
        return RedirectResponse(f"/receipts?{query}", status_code=303)
    if normalized_decision not in {
        ReceiptStatus.APPROVED.value,
        ReceiptStatus.DENIED.value,
    }:
        raise HTTPException(status_code=400, detail="Invalid receipt decision.")
    if normalized_decision == ReceiptStatus.DENIED.value and not remarks:
        query = urlencode({"error": "Rejection remarks are required when denying a receipt."})
        return RedirectResponse(f"/receipts?{query}", status_code=303)
    if len(remarks) > 1000:
        query = urlencode({"error": "Rejection remarks must be 1,000 characters or fewer."})
        return RedirectResponse(f"/receipts?{query}", status_code=303)

    reviewed_at = utc_now()
    receipt.status = normalized_decision
    receipt.rejection_remarks = (
        remarks if normalized_decision == ReceiptStatus.DENIED.value else None
    )
    receipt.reviewed_by_id = user.id
    receipt.reviewed_at = reviewed_at
    queue_node_command(
        db,
        target=receipt.franchise,
        command_type="RECEIPT_REVIEWED",
        payload={
            "receipt_id": receipt.lite_receipt_id,
            "status": normalized_decision,
            "rejection_remarks": receipt.rejection_remarks,
            "reviewed_by": user.username,
            "reviewed_at": reviewed_at.isoformat(),
        },
    )
    db.commit()
    label = "approved" if normalized_decision == ReceiptStatus.APPROVED.value else "denied"
    return RedirectResponse(
        "/receipts?" + urlencode({"message": f"Receipt {label}; Lite update queued."}),
        status_code=303,
    )
