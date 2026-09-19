"""Master QR allocation stays atomic and exposes printable franchise labels."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import NetworkStock, NodeCommand, Product, QrAllocation, Serial, User
from app.routers.qr_console import router as qr_router
from app.services.node_auth import create_franchise_node
from app.services.qr_allocation import allocate_qr_serials, get_or_create_franchise_product
from tests import test_network_sync as network_tests
from tests.factories import authenticate_client

api = network_tests.api


def test_allocation_request_replay_does_not_duplicate_serials_or_commands(db_session):
    node = create_franchise_node(db_session, code="QR-01", name="QR one", location="Main")
    product = get_or_create_franchise_product(
        db_session,
        node,
        product_code="PROD",
        product_name="Product",
        tally_stock_item_name="Product",
        hsn="1234",
        gst_rate=18,
    )
    request_id = str(uuid4())
    first = allocate_qr_serials(
        db_session,
        node=node,
        product=product,
        product_code="PROD",
        quantity=2,
        allocation_id=request_id,
    )
    db_session.commit()

    replay = allocate_qr_serials(
        db_session,
        node=node,
        product=product,
        product_code="PROD",
        quantity=2,
        allocation_id=request_id,
    )
    assert replay == first
    assert db_session.scalar(select(func.count(Serial.id))) == 2
    assert db_session.scalar(select(func.count(NetworkStock.id))) == 2
    assert db_session.scalar(select(func.count(NodeCommand.id))) == 1
    assert db_session.scalar(select(func.count(QrAllocation.id))) == 1

    with pytest.raises(ValueError, match="different QR codes"):
        allocate_qr_serials(
            db_session,
            node=node,
            product=product,
            product_code="PROD",
            quantity=3,
            allocation_id=request_id,
        )
    db_session.rollback()
    assert db_session.scalar(select(func.count(Serial.id))) == 2


def test_qr_console_requires_master_admin_and_prints_allocated_serial(api):
    client, db = api
    client.app.include_router(qr_router)
    node = create_franchise_node(db, code="QR-UI", name="QR UI", location="Main")
    admin = User(username="qr-admin", password_hash="x", role="admin")
    viewer = User(username="qr-viewer", password_hash="x", role="directors")
    db.add_all([admin, viewer])
    db.commit()
    form = {
        "request_id": str(uuid4()),
        "product_code": "UI-PROD",
        "product_name": "UI Product",
        "tally_stock_item_name": "UI Product",
        "hsn": "1234",
        "gst_rate": "18",
        "quantity": "1",
    }
    url = f"/franchises/{node.public_id}/qr"

    authenticate_client(client, viewer.id)
    assert client.post(url, data=form).status_code == 403
    assert db.scalar(select(func.count(Serial.id))) == 0

    authenticate_client(client, admin.id)
    assert client.get(url).status_code == 200
    created = client.post(url, data=form, follow_redirects=False)
    assert created.status_code == 303
    serial = db.scalar(select(Serial))
    command = db.scalar(select(NodeCommand).where(NodeCommand.command_type == "QR_ALLOCATED"))
    assert serial is not None and command is not None
    assert db.scalar(select(Product).where(Product.product_code == "QR-UI:UI-PROD")) is not None
    assert client.get(f"{url}/{command.public_id}/labels").status_code == 200
    image = client.get(f"{url}/{serial.serial_number}/image.svg")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in image.content
