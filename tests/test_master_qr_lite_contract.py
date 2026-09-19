"""Master QR allocation through the real Lite command and event services."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import NetworkStock, Serial
from app.services.qr_allocation import allocate_qr_serials, get_or_create_franchise_product
from tests import test_network_sync as network_tests

api = network_tests.api


def test_master_qr_reaches_lite_and_returns_in_purchase_event(api, tmp_path):
    client, db = api
    lite_root = Path(__file__).resolve().parents[2] / "Setuora-Lite"
    if not (lite_root / "app").is_dir():
        pytest.skip("Cross-edition check requires sibling Setuora-Lite checkout")
    python = lite_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        python = Path(sys.executable)
    helper = Path(__file__).with_name("lite_contract_worker.py")
    node, api_key = network_tests.create_node_with_key(db, "QRCONTRACT")

    def lite(action, **details):
        environment = dict(os.environ)
        environment.update(
            {
                "SETUORA_APP_MODE": "lite",
                "SETUORA_ALLOW_LEGACY_TEST_MODE": "false",
                "DATABASE_URL": f"sqlite:///{tmp_path / 'qr-lite.db'}",
                "APP_SECRET_KEY": "isolated-contract-secret-key-for-test-only",
                "FRANCHISE_CODE": node.code,
                "MASTER_SYNC_ENABLED": "true",
                "MASTER_URL": "https://master.example.test",
                "MASTER_API_KEY": api_key,
                "MASTER_CONNECTION_SETTINGS_FILE": str(tmp_path / "qr-lite.env"),
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
    assert len(initial["events"]) == 1
    heartbeat = initial["events"][0]
    accepted = client.post(
        "/api/v1/events",
        headers=network_tests.auth(api_key),
        json={"events": [heartbeat]},
    )
    assert accepted.status_code == 200, accepted.text
    lite(
        "deliver",
        responses={heartbeat["event_id"]: {"status": 200, "body": accepted.text}},
    )

    product = get_or_create_franchise_product(
        db,
        node,
        product_code="QR-PROD",
        product_name="Master QR product",
        tally_stock_item_name="Master QR product",
        hsn="2106",
        gst_rate=18,
        default_rate=100,
    )
    serial_number = allocate_qr_serials(
        db, node=node, product=product, product_code="QR-PROD", quantity=1
    )[0]
    db.commit()
    master_serial = db.scalar(select(Serial).where(Serial.serial_number == serial_number))
    assert master_serial.status == "GENERATED"
    stock = db.scalar(select(NetworkStock).where(NetworkStock.serial_id == master_serial.id))
    assert stock.current_franchise_id == node.id

    command_response = client.get("/api/v1/commands", headers=network_tests.auth(api_key))
    assert command_response.status_code == 200
    commands = command_response.json()["data"]["commands"]
    assert len(commands) == 1
    assert commands[0]["type"] == "QR_ALLOCATED"
    received = lite("commands", commands=commands)
    assert received["serials"] == {serial_number: "GENERATED"}
    assert received["events"] == []
    assert lite("commands", commands=commands) == received

    purchased = lite("purchase_allocated", serial_number=serial_number)
    assert len(purchased["events"]) == 1
    purchase_response = client.post(
        "/api/v1/events",
        headers=network_tests.auth(api_key),
        json={"events": purchased["events"]},
    )
    assert purchase_response.status_code == 200, purchase_response.text
    db.refresh(master_serial)
    db.refresh(stock)
    assert master_serial.status == "IN_STOCK"
    assert stock.status == "IN_STOCK"
