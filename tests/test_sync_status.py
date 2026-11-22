"""Sync Monitor snapshot: what has moved between Lite, Master and Tally."""

import json
from datetime import date

from app.models import Batch, BatchStatus, InboundEvent, TallyReceipt, User, utc_now
from app.services import sync_status
from app.services import tally_godown_qr as godown_qr
from app.services.node_auth import create_franchise_node
from app.services.tally_masters import TallyStockItem


def test_snapshot_reports_lite_stock_tally_queue_and_receipts(db_session, tmp_path, monkeypatch):
    node = create_franchise_node(db_session, code="CHN-01", name="Chintamani", location="Chintamani")
    node.tally_godown_name = "CHINTHAMANI WAREHOUSE"
    db_session.commit()
    items = [TallyStockItem(name="FACE WASH 100ML", unit="PCS", hsn="3304", gst_rate=18, standard_rate=120)]
    plan = godown_qr.plan_from_stock(db_session, node, {"FACE WASH 100ML": (3.0, "PCS")}, items, [])
    godown_qr.create_from_plan(db_session, node, plan, plan["token"], created_by_id=None)

    user = db_session.query(User).first() or User(username="svc", password_hash="x", role="admin")
    db_session.add(user)
    db_session.flush()
    batch = Batch(
        batch_number="CHN-SALE-1",
        batch_type="SALE",
        user_id=user.id,
        status=BatchStatus.FAILED.value,
        party_name="CASH CUSTOMER",
        last_error="Ledger not found",
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        InboundEvent(
            event_id="e1", franchise_id=node.id, sequence=1, schema_version=1, event_type="SALE",
            occurred_at=utc_now(), received_at=utc_now(), payload_json="{}", payload_hash="h",
            result_json="{}", tally_batch_id=batch.id,
        )
    )
    db_session.add(
        TallyReceipt(
            franchise_id=node.id, tally_guid="g1", voucher_type="Recipets Chintamani Warehouse",
            voucher_date=date(2026, 10, 17), total_amount=500.0, lines_json="[]",
            first_synced_at=utc_now(), last_synced_at=utc_now(),
        )
    )
    db_session.commit()

    snapshot = sync_status.build_snapshot(db_session)
    chn = snapshot["franchises"][0]
    assert chn["code"] == "CHN-01"
    assert chn["stock"] == {"AWAITING_STOCK_IN": 3}
    assert chn["to_lite"]["QR_ALLOCATED"]["waiting"] == 1
    assert chn["from_lite"]["SALE"]["count"] == 1
    assert chn["to_tally"]["by_status"] == {"FAILED": 1}
    assert chn["from_tally"]["receipts"] == 1 and chn["from_tally"]["receipts_total"] == 500.0
    waiting = snapshot["tally_waiting"]
    assert waiting[0]["franchise"] == "CHN-01" and waiting[0]["error"] == "Ledger not found"

    # The file holds no credentials and is replaced atomically.
    monkeypatch.setenv("SETUORA_STATUS_DIR", str(tmp_path))
    monkeypatch.setattr(sync_status, "SessionLocal", lambda: _Use(db_session))
    path = sync_status.write_snapshot()
    text = path.read_text(encoding="utf-8")
    assert json.loads(text)["franchises"][0]["code"] == "CHN-01"
    assert "password" not in text.lower() and "credential" not in text.lower()


class _Use:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False
