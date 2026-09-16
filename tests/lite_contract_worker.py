"""Subprocess helper: exercise real Lite services without importing Master's app."""

import json
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path.cwd()))

from app.services.inventory import add_serial_to_batch, apply_batch_statuses, create_batch
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import (
    BatchStatus,
    BatchType,
    LocalTransfer,
    MasterOutboxEvent,
    Product,
    Serial,
    SerialStatus,
    User,
)
from app.services import master_sync, transfer


def main():
    instruction = json.load(sys.stdin)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        user = db.scalar(select(User))
        if user is None:
            user = User(username="contract-admin", password_hash="unused", role="admin")
            db.add(user)
            db.commit()
        action = instruction["action"]
        if action == "initialize":
            if instruction.get("stock"):
                product = Product(
                    product_code="CONTRACT-SKU",
                    product_name="Contract product",
                    tally_stock_item_name="Contract product",
                    hsn="2106",
                    unit="Pcs",
                    gst_rate=18,
                    default_rate=118,
                    sales_discount_rate=10,
                )
                db.add(product)
                db.flush()
                for suffix in ("SALE", "TRANSFER-1", "TRANSFER-2"):
                    db.add(
                        Serial(
                            serial_number=f"SOURCE-{suffix}",
                            product_id=product.id,
                            status=SerialStatus.IN_STOCK.value,
                            warehouse="MAIN",
                            active=True,
                        )
                    )
                db.commit()
            master_sync.enqueue_initial_inventory_snapshot(db, actor=user)
        elif action == "operations":
            batch = create_batch(db, user, BatchType.SALE, "Contract customer", "Contract sale")
            add_serial_to_batch(db, batch, user, "SOURCE-SALE")
            apply_batch_statuses(db, batch, user)
            batch.status = BatchStatus.PENDING_SYNC.value
            master_sync.enqueue_batch_submitted_event(db, batch, user=user)
            db.commit()
            outgoing = transfer.create_outbound_transfer(db, user, "DEST")
            for suffix in ("1", "2"):
                transfer.add_outbound_serial(db, outgoing, f"SOURCE-TRANSFER-{suffix}")
            transfer.dispatch_outbound_transfer(db, outgoing, user)
        elif action == "deliver":
            for row in db.scalars(select(MasterOutboxEvent)).all():
                if row.next_attempt_at is not None:
                    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()

            def opener(request, **_kwargs):
                if instruction.get("lose_response"):
                    raise URLError("Simulated connection lost after Master committed")
                event_id = json.loads(request.data)["events"][0]["event_id"]
                response = instruction["responses"][event_id]
                body = BytesIO(response["body"].encode())
                body.status = response["status"]
                return body

            master_sync.push_pending_events(db, opener=opener)
        elif action == "commands":
            for command in instruction["commands"]:
                master_sync.apply_master_command(db, command)
                db.commit()
            if instruction.get("receive"):
                incoming = db.scalar(select(LocalTransfer))
                transfer.scan_inbound_transfer_item(db, incoming, instruction["receive"])
                transfer.finalize_inbound_receipt(db, incoming, user)
        elif action != "inspect":
            raise ValueError(action)
        result = {
            "events": [
                json.loads(row.payload_json)["events"][0]
                for row in db.scalars(select(MasterOutboxEvent).order_by(MasterOutboxEvent.id))
                if row.status != "SENT"
            ],
            "event_statuses": [
                row.status
                for row in db.scalars(select(MasterOutboxEvent).order_by(MasterOutboxEvent.id))
            ],
            "serials": {row.serial_number: row.status for row in db.scalars(select(Serial))},
            "transfers": [row.status for row in db.scalars(select(LocalTransfer))],
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
