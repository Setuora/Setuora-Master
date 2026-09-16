"""Master onboarding output must configure an independently restarted Lite."""

import json
import os
import queue
import re
import subprocess
import sys
import threading
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import FranchiseNode, User
from app.routers.master_console import router as master_router
from tests import test_network_sync as network_tests
from tests.factories import authenticate_client

api = network_tests.api


def copied_bundle(response):
    match = re.search(r'<textarea id="lite-setup-details"[^>]*>(.*?)</textarea>', response.text, re.S)
    assert match is not None
    return json.loads(unescape(match[1]))


@pytest.fixture()
def setup_contract(api, tmp_path):
    client, db = api
    client.app.include_router(master_router)
    admin = User(username="setup-master-admin", password_hash="unused", role="admin")
    db.add(admin)
    db.commit()
    authenticate_client(client, admin.id)
    saved = client.post(
        "/franchises/connection-address", data={"master_url": "https://warehouse.example.test"}
    )
    assert saved.status_code == 200

    lite_root = Path(__file__).resolve().parents[2] / "Setuora-Lite"
    if not (lite_root / "app").is_dir():
        pytest.skip("Setup contract requires sibling Setuora-Lite checkout")
    python = lite_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        python = Path(sys.executable)

    def lite(action, *, profile="primary", **details):
        env = dict(os.environ)
        env.update(
            {
                "SETUORA_APP_MODE": "lite",
                "SETUORA_ALLOW_LEGACY_TEST_MODE": "false",
                "DATABASE_URL": f"sqlite:///{tmp_path / (profile + '.db')}",
                "APP_SECRET_KEY": "isolated-setup-contract-secret-key-for-test-only",
                "FRANCHISE_CODE": "",
                "MASTER_SYNC_ENABLED": "false",
                "MASTER_URL": "",
                "MASTER_API_KEY": "",
                "MASTER_CONNECTION_SETTINGS_FILE": str(tmp_path / (profile + ".env")),
                "BACKUP_SETTINGS_FILE": str(tmp_path / "backup.env"),
                "SFTP_CONNECTION_SETTINGS_FILE": str(tmp_path / "sftp.env"),
                "AUTOMATIC_BACKUPS_ENABLED": "false",
                "SFTP_SYNC_ENABLED": "false",
            }
        )
        process = subprocess.Popen(  # noqa: S603 - fixed local helper, no shell
            [str(python), str(Path(__file__).with_name("connection_setup_worker.py"))],
            cwd=lite_root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = queue.Queue()

        def read_output():
            for line in process.stdout:
                output.put(line)
            output.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        try:
            process.stdin.write(json.dumps({"action": action, **details}) + "\n")
            process.stdin.flush()
            result = None
            while result is None:
                line = output.get(timeout=30)
                if line is None:
                    raise AssertionError(process.stderr.read())
                message = json.loads(line)
                if message["kind"] == "result":
                    result = message["data"]
                    continue
                assert message["kind"] == "master_request"
                response = client.request(
                    message["method"],
                    message["path"],
                    content=message["body"],
                    headers={
                        **network_tests.auth(details["expected_credential"]),
                        "Content-Type": "application/json",
                    },
                )
                process.stdin.write(
                    json.dumps({"status": response.status_code, "body": response.text}) + "\n"
                )
                process.stdin.flush()
            process.wait(timeout=10)
            assert process.returncode == 0, process.stderr.read()
            return result
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def create_bundle(code):
        response = client.post(
            "/franchises",
            data={
                "code": code,
                "name": f"Franchise {code}",
                "location": "Warehouse",
                "tally_godown_name": f"Godown {code}",
            },
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        bundle = copied_bundle(response)
        assert set(bundle) == {
            "format",
            "version",
            "master_url",
            "franchise_code",
            "node_credential",
        }
        assert bundle["format"] == "setuora-lite-connection"
        assert bundle["version"] == 1
        assert bundle["node_credential"] not in client.get("/franchises").text
        return bundle

    def master_node(bundle):
        response = client.get("/api/v1/node", headers=network_tests.auth(bundle["node_credential"]))
        return {"status": response.status_code, "body": response.text}

    return client, db, lite, create_bundle, master_node


def test_master_bundle_connects_lite_after_restart_and_supports_credential_rotation(setup_contract):
    client, db, lite, create_bundle, master_node = setup_contract
    bundle = create_bundle("WAREHOUSE-01")
    node = db.scalar(select(FranchiseNode).where(FranchiseNode.code == bundle["franchise_code"]))
    details = {"expected_credential": bundle["node_credential"]}
    connected = lite("connect", bundle=bundle, **details)
    assert connected["status"] == 303
    assert "error" not in parse_qs(urlsplit(connected["location"]).query)
    assert connected["bound_node_id"] == node.public_id
    assert len(connected["events"]) == 1
    db.refresh(node)
    assert node.last_sequence == 1

    restarted = lite("inspect", **details)
    assert restarted["status"] == 200
    assert restarted["franchise_code"] == "WAREHOUSE-01"
    assert restarted["master_url"] == bundle["master_url"]
    assert restarted["credential_matches"] is True
    assert restarted["enabled"] is True

    replaced = client.post(
        f"/franchises/{node.public_id}/credentials", data={"confirm_replace": "true"}
    )
    assert replaced.status_code == 200
    replacement = copied_bundle(replaced)
    assert master_node(bundle)["status"] == 401
    reconnected = lite(
        "connect",
        bundle=replacement,
        expected_credential=replacement["node_credential"],
    )
    assert "error" not in parse_qs(urlsplit(reconnected["location"]).query)
    assert reconnected["bound_node_id"] == node.public_id
    assert len(reconnected["events"]) == 1

    # A rebuilt Master can reuse a code/cursor. Its new node UUID must not be
    # mistaken for the installation that owns this Lite's existing history.
    node.public_id = str(uuid4())
    db.commit()
    wrong_identity = lite(
        "connect",
        bundle=replacement,
        expected_credential=replacement["node_credential"],
    )
    assert "error" in parse_qs(urlsplit(wrong_identity["location"]).query)
    assert wrong_identity["bound_node_id"] == connected["bound_node_id"]


def test_setup_rejects_credential_for_a_different_franchise_before_saving(setup_contract):
    _, _, lite, create_bundle, _master_node = setup_contract
    intended = create_bundle("WAREHOUSE-01")
    other = create_bundle("WAREHOUSE-02")
    incorrect = {**intended, "node_credential": other["node_credential"]}
    rejected = lite(
        "connect",
        bundle=incorrect,
        expected_credential=other["node_credential"],
    )
    assert rejected["status"] == 303
    assert "error" in parse_qs(urlsplit(rejected["location"]).query)
    assert rejected["franchise_code"] == ""
    assert rejected["credential_matches"] is False
    assert rejected["bound_node_id"] is None
    assert rejected["events"] == []
