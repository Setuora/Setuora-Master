import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import create_app
from app.models import FranchiseNode, NodeCredential, User
from app.services.lite_connection import MASTER_URL_SETTING
from app.services.settings import get_setting
from tests.factories import authenticate_client


@pytest.fixture()
def console():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all(
        [
            User(id=1, username="admin", password_hash="unused", role="admin"),
            User(id=2, username="director", password_hash="unused", role="directors"),
        ]
    )
    session.commit()
    app = create_app("master")

    def override():
        yield session

    app.dependency_overrides[get_db] = override
    client = TestClient(app)
    authenticate_client(client, 1)
    try:
        yield client, session
    finally:
        client.close()
        session.close()
        engine.dispose()


def post(client, path, data):
    return client.post(
        path, data=data, headers={"Origin": "http://testserver"}, follow_redirects=False
    )


def bundle(response):
    value = re.search(
        r'<textarea id="lite-setup-details"[^>]*>(.*?)</textarea>', response.text, re.S
    )
    assert value, response.text
    return json.loads(html.unescape(value.group(1)))


def add_franchise(client, **overrides):
    return post(
        client,
        "/franchises",
        {
            "code": "BLR-01",
            "name": "Bengaluru warehouse",
            "location": "Bengaluru",
            **overrides,
        },
    )


def save_address(client):
    return post(
        client, "/franchises/connection-address", {"master_url": "https://master.example.com"}
    )


def test_create_franchise_returns_a_single_complete_working_connection_bundle(console):
    client, db = console
    assert save_address(client).status_code == 200
    response = add_franchise(client)
    assert response.status_code == 200
    details = bundle(response)
    assert details["format"] == "setuora-lite-connection"
    assert details["version"] == 1
    assert details["franchise_code"] == "BLR-01"
    assert details["master_url"] == "https://master.example.com"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "same-origin"
    credential = details["node_credential"]
    node_response = client.get("/api/v1/node", headers={"Authorization": f"Bearer {credential}"})
    assert node_response.status_code == 200
    assert node_response.json()["data"]["code"] == "BLR-01"
    assert credential not in client.get("/franchises").text
    assert db.scalar(select(func.count()).select_from(NodeCredential)) == 1
    assert db.scalar(select(NodeCredential)).secret_hash != credential.split(".")[-1]


def test_creation_requires_address_and_keeps_form_values_on_error(console):
    client, db = console
    response = add_franchise(client)
    assert response.status_code == 400
    assert "Save the Master HTTPS address" in response.text
    assert 'value="Bengaluru warehouse"' in response.text
    assert db.scalar(select(func.count()).select_from(FranchiseNode)) == 0


@pytest.mark.parametrize(
    "address",
    [
        "http://master.example.com",
        "https://master.example.com/api/v1",
        "https://master.example.com:8000",
        "https://user:secret@master.example.com",
        "https://master.example.com?key=secret",
        "https://master.example.com/#fragment",
        "https://master.example.com\n.attacker.example",
    ],
)
def test_invalid_address_does_not_replace_saved_address_or_echo_secret(console, address):
    client, db = console
    save_address(client)
    response = post(client, "/franchises/connection-address", {"master_url": address})
    assert response.status_code == 400
    assert get_setting(db, MASTER_URL_SETTING) == "https://master.example.com"
    assert "user:secret" not in response.text


@pytest.mark.parametrize("code", ["LITE", "MASTER", "DEFAULT", "BAD CODE", "X" * 21])
def test_franchise_codes_must_be_usable_by_lite(console, code):
    client, db = console
    save_address(client)
    assert add_franchise(client, code=code).status_code == 400
    assert db.scalar(select(func.count()).select_from(NodeCredential)) == 0


def test_duplicate_enrollment_keeps_original_connection(console):
    client, db = console
    save_address(client)
    credential = bundle(add_franchise(client))["node_credential"]
    assert add_franchise(client).status_code == 400
    assert db.scalar(select(func.count()).select_from(NodeCredential)) == 1
    assert (
        client.get("/api/v1/node", headers={"Authorization": f"Bearer {credential}"}).status_code
        == 200
    )


def test_replacing_details_requires_confirmation_and_revokes_only_that_franchise(console):
    client, db = console
    save_address(client)
    old = bundle(add_franchise(client))["node_credential"]
    node = db.scalar(select(FranchiseNode))
    path = f"/franchises/{node.public_id}/credentials"
    assert post(client, path, {}).status_code == 400
    assert client.get("/api/v1/node", headers={"Authorization": f"Bearer {old}"}).status_code == 200
    replacement = bundle(post(client, path, {"confirm_replace": "true"}))["node_credential"]
    assert replacement != old
    assert client.get("/api/v1/node", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert (
        client.get("/api/v1/node", headers={"Authorization": f"Bearer {replacement}"}).status_code
        == 200
    )


def test_connection_administration_is_admin_only_and_requires_same_origin(console):
    client, _db = console
    assert (
        client.post(
            "/franchises/connection-address", data={"master_url": "https://master.example.com"}
        ).status_code
        == 403
    )
    authenticate_client(client, 2)
    assert save_address(client).status_code == 403
    assert add_franchise(client).status_code == 403


def test_disable_access_requires_confirmation_and_explains_reconnection(console):
    client, db = console
    save_address(client)
    credential = bundle(add_franchise(client))["node_credential"]
    node = db.scalar(select(FranchiseNode))
    path = f"/franchises/{node.public_id}/status"
    assert post(client, path, {"active": "false"}).status_code == 400
    assert node.active is True
    assert post(client, path, {"active": "false", "confirm_deactivate": "true"}).status_code == 200
    assert node.active is False
    restored = post(client, path, {"active": "true"})
    assert "Create replacement connection details and paste them into Lite" in restored.text
    assert (
        client.get("/api/v1/node", headers={"Authorization": f"Bearer {credential}"}).status_code
        == 401
    )
