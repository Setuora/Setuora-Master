import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import SESSION_COOKIE
from app.database import Base, get_db
from app.models import NodeCommand, Receipt, ReceiptStatus, User
from app.routers.node_api import router as node_api_router
from app.routers.receipts import router as receipt_router
from app.security import create_session_token
from app.services.node_auth import create_franchise_node, provision_node_credential


def test_lite_receipt_sync_and_master_denial_workflow():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    node = create_franchise_node(db, code="RCPT-01", name="RECEIPT FRANCHISE", location="BENGALURU")
    credential = provision_node_credential(db, node)
    admin = User(id=100, username="master-admin", password_hash="x", role="admin")
    db.add(admin)
    db.commit()

    app = FastAPI()
    app.include_router(node_api_router)
    app.include_router(receipt_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, follow_redirects=False)
    proof = b"\x89PNG\r\n\x1a\n" + b"receipt-proof"
    receipt_uuid = str(uuid4())
    event_uuid = str(uuid4())
    response = client.post(
        "/api/v1/events",
        headers={"Authorization": f"Bearer {credential.api_key}"},
        json={
            "events": [
                {
                    "event_id": event_uuid,
                    "sequence": 1,
                    "schema_version": 1,
                    "type": "RECEIPT_SUBMITTED",
                    "occurred_at": datetime(2026, 8, 17, 10, 0, tzinfo=UTC).isoformat(),
                    "reference": receipt_uuid,
                    "actor": "lite-sales",
                    "items": [],
                    "receipt_id": receipt_uuid,
                    "receipt_date": "2026-08-17",
                    "proof_content_type": "image/png",
                    "proof_image_base64": base64.b64encode(proof).decode("ascii"),
                    "utr_number": "UTR12345",
                }
            ]
        },
    )
    assert response.status_code == 200
    receipt = db.scalar(select(Receipt))
    assert receipt is not None
    assert receipt.franchise_id == node.id
    assert receipt.proof_image == proof
    assert receipt.utr_number == "UTR12345"
    assert receipt.status == ReceiptStatus.PENDING.value

    client.cookies.set(SESSION_COOKIE, create_session_token(admin.id))
    page = client.get("/receipts")
    image = client.get(f"/receipts/{receipt.public_id}/proof")
    missing_remarks = client.post(
        f"/receipts/{receipt.public_id}/review", data={"decision": "DENIED"}
    )
    db.refresh(receipt)
    assert page.status_code == 200
    assert "UTR12345" in page.text
    assert image.status_code == 200
    assert image.content == proof
    assert missing_remarks.status_code == 303
    assert "error=" in missing_remarks.headers["location"]
    assert receipt.status == ReceiptStatus.PENDING.value

    denied = client.post(
        f"/receipts/{receipt.public_id}/review",
        data={"decision": "DENIED", "rejection_remarks": "Proof is unreadable"},
    )
    db.refresh(receipt)
    command = db.scalar(select(NodeCommand))
    assert denied.status_code == 303
    assert receipt.status == ReceiptStatus.DENIED.value
    assert receipt.rejection_remarks == "Proof is unreadable"
    assert command is not None
    assert command.command_type == "RECEIPT_REVIEWED"
    assert json.loads(command.payload_json)["receipt_id"] == receipt_uuid

    approved_uuid = str(uuid4())
    approved_event = client.post(
        "/api/v1/events",
        headers={"Authorization": f"Bearer {credential.api_key}"},
        json={
            "events": [
                {
                    "event_id": str(uuid4()),
                    "sequence": 2,
                    "schema_version": 1,
                    "type": "RECEIPT_SUBMITTED",
                    "occurred_at": datetime(2026, 8, 17, 11, 0, tzinfo=UTC).isoformat(),
                    "reference": approved_uuid,
                    "actor": "lite-sales",
                    "items": [],
                    "receipt_id": approved_uuid,
                    "receipt_date": "2026-08-17",
                    "proof_content_type": "image/png",
                    "proof_image_base64": base64.b64encode(proof).decode("ascii"),
                    "utr_number": None,
                }
            ]
        },
    )
    approved_receipt = db.scalar(select(Receipt).where(Receipt.lite_receipt_id == approved_uuid))
    approved = client.post(
        f"/receipts/{approved_receipt.public_id}/review", data={"decision": "APPROVED"}
    )
    db.refresh(approved_receipt)
    assert approved_event.status_code == 200
    assert approved.status_code == 303
    assert approved_receipt.status == ReceiptStatus.APPROVED.value
    assert approved_receipt.rejection_remarks is None

    client.close()
    db.close()
    engine.dispose()


def test_receipt_event_rejects_mismatched_image_content_type():
    from pydantic import ValidationError

    from app.network_schemas import NetworkEventV1

    try:
        NetworkEventV1.model_validate(
            {
                "event_id": str(uuid4()),
                "sequence": 1,
                "type": "RECEIPT_SUBMITTED",
                "occurred_at": datetime.now(UTC).isoformat(),
                "items": [],
                "receipt_id": str(uuid4()),
                "receipt_date": "2026-08-17",
                "proof_content_type": "image/jpeg",
                "proof_image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nproof").decode("ascii"),
            }
        )
    except ValidationError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("Expected mismatched image content to be rejected")
