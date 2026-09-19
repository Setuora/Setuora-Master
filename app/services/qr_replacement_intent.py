"""Master-owned QR replacement requests and their durable reservations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base
from app.models import FranchiseNode, NetworkStock, NodeCommand, Serial, SerialStatus, utc_now
from app.services.network_ingest import queue_node_command
from app.services.qr_allocation import new_qr_serial

REPLACEABLE_STATUSES = {
    SerialStatus.GENERATED.value,
    SerialStatus.IN_STOCK.value,
    SerialStatus.RETURNED.value,
    SerialStatus.DAMAGED.value,
}


class QrReplacementIntent(Base):
    """One reserved new QR for one old QR, consumed by the matching Lite event."""

    __tablename__ = "qr_replacement_intents"
    __table_args__ = (
        UniqueConstraint("old_serial_id", name="uq_qr_replacement_old_serial"),
        UniqueConstraint("new_serial_number", name="uq_qr_replacement_new_serial"),
        UniqueConstraint("command_id", name="uq_qr_replacement_command"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    franchise_id: Mapped[int] = mapped_column(ForeignKey("franchise_nodes.id"), index=True)
    old_serial_id: Mapped[int] = mapped_column(ForeignKey("serials.id"), index=True)
    new_serial_number: Mapped[str] = mapped_column(String(140))
    expected_status: Mapped[str] = mapped_column(String(40))
    command_id: Mapped[int] = mapped_column(ForeignKey("node_commands.id"))
    requested_by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QrReplacementError(ValueError):
    pass


def request_qr_replacement(
    db: Session,
    *,
    franchise: FranchiseNode,
    old_serial_number: str,
    requested_by: str,
    reason: str = "",
) -> tuple[QrReplacementIntent, NodeCommand]:
    """Reserve a new identity and command in the caller's transaction.

    The replacement Serial and NetworkStock are deliberately created only
    after Lite commits the change and uploads its QR_REPLACEMENT stock event.
    """

    if not franchise.active:
        raise QrReplacementError("This franchise is disabled.")
    old_code = old_serial_number.strip().upper()
    if not old_code:
        raise QrReplacementError("Enter the old QR serial number.")
    if len(reason.strip()) > 500:
        raise QrReplacementError("Reason must be 500 characters or fewer.")
    old = db.scalar(select(Serial).where(Serial.serial_number == old_code))
    if old is None:
        raise QrReplacementError("Old QR serial was not found on Master.")
    stock = db.scalar(select(NetworkStock).where(NetworkStock.serial_id == old.id))
    if stock is None or stock.current_franchise_id != franchise.id:
        raise QrReplacementError("Old QR serial is not owned by this franchise.")
    if (
        not old.active
        or old.replaced_by_id is not None
        or stock.status not in REPLACEABLE_STATUSES
        or old.status != stock.status
    ):
        raise QrReplacementError("Old QR serial is not available for replacement.")
    if db.scalar(
        select(QrReplacementIntent.id).where(QrReplacementIntent.old_serial_id == old.id)
    ) is not None:
        raise QrReplacementError("A replacement has already been requested for this QR.")

    # QR_ALLOCATED identities already have Serial rows. A second unique index
    # below reserves replacement identities that have not become Serial rows.
    for _ in range(5):
        new_code = new_qr_serial()
        if db.scalar(select(Serial.id).where(Serial.serial_number == new_code)) is None and db.scalar(
            select(QrReplacementIntent.id).where(QrReplacementIntent.new_serial_number == new_code)
        ) is None:
            break
    else:
        raise QrReplacementError("Could not reserve a unique QR serial. Try again.")

    command = queue_node_command(
        db,
        target=franchise,
        command_type="QR_REPLACE",
        payload={
            "version": 1,
            "franchise_code": franchise.code,
            "old_serial_number": old.serial_number,
            "new_serial_number": new_code,
            "expected_status": stock.status,
            "reason": reason.strip(),
        },
    )
    intent = QrReplacementIntent(
        franchise_id=franchise.id,
        old_serial_id=old.id,
        new_serial_number=new_code,
        expected_status=stock.status,
        command_id=command.id,
        requested_by=requested_by[:120],
        reason=reason.strip(),
    )
    db.add(intent)
    db.flush()
    return intent, command


def validate_replacement_intent(
    db: Session,
    *,
    franchise: FranchiseNode,
    command_public_id: str | None,
    old_serial: Serial,
    old_stock: NetworkStock,
    new_serial_number: str,
) -> QrReplacementIntent:
    """Require the Lite event to consume its exact Master-issued reservation."""

    from app.services.network_ingest import NetworkIngestError

    command = db.scalar(
        select(NodeCommand).where(
            NodeCommand.public_id == (command_public_id or ""),
            NodeCommand.target_franchise_id == franchise.id,
            NodeCommand.command_type == "QR_REPLACE",
        )
    )
    intent = (
        db.scalar(select(QrReplacementIntent).where(QrReplacementIntent.command_id == command.id))
        if command is not None
        else None
    )
    if (
        intent is None
        or intent.completed_at is not None
        or intent.franchise_id != franchise.id
        or intent.old_serial_id != old_serial.id
        or intent.new_serial_number != new_serial_number
        or intent.expected_status != old_stock.status
    ):
        raise NetworkIngestError(
            409,
            "QR_REPLACEMENT_NOT_RESERVED",
            "This replacement does not match a pending Master QR request.",
        )
    return intent
