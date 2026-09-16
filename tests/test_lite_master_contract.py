"""Cross-repository contract/recovery test with independent, persistent Lite DBs.

Transport is an in-process Master ASGI client; this does not certify deployed TLS
or a real Tally company. Each Lite action starts a fresh Python process.
"""

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import Batch, InboundEvent, NetworkStock, NodeCommand, StockTransfer
from app.services.voucher import calculate_voucher_summary
from tests import test_network_sync as network_tests

api = network_tests.api
auth = network_tests.auth
create_node_with_key = network_tests.create_node_with_key


def test_two_lites_restart_replay_sale_and_partial_transfer(api, tmp_path):
    client, db = api
    lite_root = Path(__file__).resolve().parents[2] / "Setuora-Lite"
    if not (lite_root / "app").is_dir():
        pytest.skip("Cross-edition check requires sibling Setuora-Lite checkout")
    python = lite_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        python = Path(sys.executable)
    helper = Path(__file__).with_name("lite_contract_worker.py")
    source, source_key = create_node_with_key(db, "SOURCE")
    destination, destination_key = create_node_with_key(db, "DEST")
    # The helper uses permanent plain codes; API credentials bind them to nodes.
    source.code = "SOURCE"
    destination.code = "DEST"
    source.tally_godown_name = "Source godown"
    db.commit()

    def lite(code, action, **details):
        env = dict(os.environ)
        env.update(
            {
                "SETUORA_APP_MODE": "lite",
                "SETUORA_ALLOW_LEGACY_TEST_MODE": "false",
                "DATABASE_URL": f"sqlite:///{tmp_path / (code + '.db')}",
                "APP_SECRET_KEY": "isolated-contract-secret-key-for-test-only",
                "FRANCHISE_CODE": code,
                "MASTER_SYNC_ENABLED": "true",
                "MASTER_URL": "https://master.example.test",
                "MASTER_API_KEY": source_key if code == "SOURCE" else destination_key,
                "MASTER_CONNECTION_SETTINGS_FILE": str(tmp_path / (code + ".env")),
                "AUTOMATIC_BACKUPS_ENABLED": "false",
                "SFTP_SYNC_ENABLED": "false",
            }
        )
        completed = subprocess.run(  # noqa: S603 - fixed local test helper, no shell
            [str(python), str(helper)],
            cwd=lite_root,
            env=env,
            input=json.dumps({"action": action, **details}),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def accept(code, state):
        key = source_key if code == "SOURCE" else destination_key
        responses = {}
        for event in state["events"]:
            response = client.post("/api/v1/events", headers=auth(key), json={"events": [event]})
            assert response.status_code == 200, response.text
            responses[event["event_id"]] = {"status": response.status_code, "body": response.text}
        return lite(code, "deliver", responses=responses)

    initial = lite("SOURCE", "initialize", stock=True)
    # Master commits, the reply disappears, and Lite restarts with its outbox intact.
    first = client.post(
        "/api/v1/events", headers=auth(source_key), json={"events": initial["events"]}
    )
    assert first.status_code == 200, first.text
    lost = lite("SOURCE", "deliver", lose_response=True)
    assert len(lost["events"]) == 1
    delivered = accept("SOURCE", lite("SOURCE", "inspect"))
    assert delivered["event_statuses"] == ["SENT"]
    assert db.scalar(select(func.count()).select_from(InboundEvent)) == 1
    assert db.scalar(select(func.count()).select_from(NetworkStock)) == 3
    accept("DEST", lite("DEST", "initialize"))
    accept("SOURCE", lite("SOURCE", "operations"))
    sale = db.scalar(select(Batch).where(Batch.batch_type == "SALE"))
    assert sale.status == "PENDING_SYNC"
    assert sale.tally_stock_location == "Source godown"
    assert calculate_voucher_summary(sale).final_value == Decimal("106.00")

    commands = client.get("/api/v1/commands", headers=auth(destination_key)).json()["data"][
        "commands"
    ]
    partial = lite("DEST", "commands", commands=commands, receive="SOURCE-TRANSFER-1")
    accept("DEST", partial)
    transfer = db.scalar(select(StockTransfer))
    db.refresh(transfer)
    assert transfer.status == "PARTIALLY_RECEIVED"
    # Redelivery after restart must not reset the already received serial.
    complete = lite("DEST", "commands", commands=commands, receive="SOURCE-TRANSFER-2")
    accept("DEST", complete)
    db.refresh(transfer)
    assert transfer.status == "RECEIVED"
    stocks = {row.serial.serial_number: row for row in db.scalars(select(NetworkStock))}
    for suffix in ("1", "2"):
        assert stocks[f"SOURCE-TRANSFER-{suffix}"].current_franchise_id == destination.id
        assert stocks[f"SOURCE-TRANSFER-{suffix}"].status == "IN_STOCK"
    receipts = client.get("/api/v1/commands", headers=auth(source_key)).json()["data"]["commands"]
    final = lite("SOURCE", "commands", commands=receipts)
    assert final["transfers"] == ["RECEIVED"]
    assert lite("SOURCE", "commands", commands=receipts) == final
    for key, rows in ((source_key, receipts), (destination_key, commands)):
        for row in rows:
            response = client.patch(
                f"/api/v1/commands/{row['command_id']}",
                headers=auth(key),
                json={"acknowledged": True},
            )
            assert response.status_code == 200
    assert all(row.acknowledged_at is not None for row in db.scalars(select(NodeCommand)))
