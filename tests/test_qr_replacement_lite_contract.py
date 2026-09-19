"""Master-issued replacement survives the real Lite command and event path."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import NetworkStock, Serial
from app.services.qr_allocation import allocate_qr_serials, get_or_create_franchise_product
from app.services.qr_replacement_intent import QrReplacementIntent, request_qr_replacement
from tests import test_network_sync as network_tests

api = network_tests.api


def test_master_replacement_reaches_lite_and_returns_as_reserved_event(api, tmp_path):
    client, db = api
    lite_root = Path(__file__).resolve().parents[2] / "Setuora-Lite"
    if not (lite_root / "app").is_dir():
        pytest.skip("Cross-edition check requires sibling Setuora-Lite checkout")
    python = lite_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        python = Path(sys.executable)
    helper = Path(__file__).with_name("lite_contract_worker.py")
    node, api_key = network_tests.create_node_with_key(db, "QRREPLACE")

    def lite(action, **details):
        environment = dict(os.environ)
        environment.update(
            {
                "SETUORA_APP_MODE": "lite",
                "SETUORA_ALLOW_LEGACY_TEST_MODE": "false",
                "DATABASE_URL": f"sqlite:///{tmp_path / 'replacement-lite.db'}",
                "APP_SECRET_KEY": "isolated-contract-secret-key-for-test-only",
                "FRANCHISE_CODE": node.code,
                "MASTER_SYNC_ENABLED": "true",
                "MASTER_URL": "https://master.example.test",
                "MASTER_API_KEY": api_key,
                "MASTER_CONNECTION_SETTINGS_FILE": str(tmp_path / "replacement-lite.env"),
                "AUTOMATIC_BACKUPS_ENABLED": "false",
                "SFTP_SYNC_ENABLED": "false",
            }
        )
        completed = subprocess.run(  # noqa: S603 - fixed local test helper, no shell
            [str(python), str(helper)],
            cwd=lite_root,
            env=environment,
            input=json.dumps({"action": action, **details}),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    initial = lite("initialize")
    heartbeat = initial["events"][0]
    accepted = client.post(
        "/api/v1/events",
        headers=network_tests.auth(api_key),
        json={"events": [heartbeat]},
    )
    assert accepted.status_code == 200, accepted.text
    lite("deliver", responses={heartbeat["event_id"]: {"status": 200, "body": accepted.text}})

    product = get_or_create_franchise_product(
        db,
        node,
        product_code="QR-REPLACE-PROD",
        product_name="Replacement product",
        tally_stock_item_name="Replacement product",
        hsn="2106",
        gst_rate=18,
    )
    old_code = allocate_qr_serials(
        db, node=node, product=product, product_code="QR-REPLACE-PROD", quantity=1
    )[0]
    db.commit()
    first_commands = client.get("/api/v1/commands", headers=network_tests.auth(api_key)).json()[
        "data"
    ]["commands"]
    allocation = next(command for command in first_commands if command["type"] == "QR_ALLOCATED")
    assert lite("commands", commands=[allocation])["serials"][old_code] == "GENERATED"

    intent, command = request_qr_replacement(
        db,
        franchise=node,
        old_serial_number=old_code,
        requested_by="master-admin",
        reason="Damaged label",
    )
    new_code = intent.new_serial_number
    db.commit()
    replacement_command = next(
        row
        for row in client.get("/api/v1/commands", headers=network_tests.auth(api_key)).json()[
            "data"
        ]["commands"]
        if row["command_id"] == command.public_id
    )
    applied = lite("commands", commands=[replacement_command])
    assert applied["serials"][old_code] == "INVALID"
    assert applied["serials"][new_code] == "GENERATED"
    assert len(applied["events"]) == 1
    replacement_event = applied["events"][0]
    assert replacement_event["reference"] == command.public_id
    assert replacement_event["reason_code"] == "QR_REPLACEMENT"
    assert {item["serial_number"]: item["status"] for item in replacement_event["items"]} == {
        old_code: "INVALID",
        new_code: "GENERATED",
    }
    assert lite("commands", commands=[replacement_command]) == applied

    accepted = client.post(
        "/api/v1/events",
        headers=network_tests.auth(api_key),
        json={"events": [replacement_event]},
    )
    assert accepted.status_code == 200, accepted.text
    old = db.scalar(select(Serial).where(Serial.serial_number == old_code))
    new = db.scalar(select(Serial).where(Serial.serial_number == new_code))
    db.refresh(old)
    assert old.status == "INVALID"
    assert old.replaced_by_id == new.id
    assert db.scalar(select(NetworkStock).where(NetworkStock.serial_id == new.id)).current_franchise_id == node.id
    assert db.get(QrReplacementIntent, intent.id).completed_at is not None
