import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import create_app
from app.models import User
from tests.factories import authenticate_client


def application_paths(app) -> set[str]:
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    for included in app.routes:
        router = getattr(included, "original_router", None)
        if router is not None:
            paths.update(route.path for route in router.routes if hasattr(route, "path"))
    return paths


def test_master_composition_exposes_monitoring_and_node_api_only():
    app = create_app("master")
    paths = application_paths(app)

    assert "/api/v1/events" in paths
    assert "/api/v1/commands" in paths
    assert "/network/events" in paths
    assert "/network/transfers" in paths
    assert "/network/reports" in paths
    assert "/network/tally" in paths
    assert "/network/tally-parties" in paths
    assert "/franchises" in paths

    assert "/batches" not in paths
    assert "/barcode-assignment" not in paths
    assert "/products" not in paths
    assert "/serials" not in paths
    assert "/warehouse/move" not in paths
    assert "/docs" not in paths
    assert "/openapi.json" not in paths


@pytest.mark.parametrize("mode", ["lite", "legacy", "test"])
def test_master_artifact_cannot_be_switched_to_other_compositions(mode):
    with pytest.raises(RuntimeError, match="only supports master mode"):
        create_app(mode)


def test_master_console_separates_monitor_and_sensitive_roles():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add_all(
            [
                User(id=1, username="director", password_hash="x", role="directors"),
                User(id=2, username="sales", password_hash="x", role="sales"),
                User(id=3, username="admin", password_hash="x", role="admin"),
            ]
        )
        db.commit()

    app = create_app("master")

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=False)
    try:
        authenticate_client(client, 1)
        assert client.get("/").status_code == 200
        authenticate_client(client, 2)
        assert client.get("/").status_code == 403
        authenticate_client(client, 1)
        assert client.get("/network/events").status_code == 403
        authenticate_client(client, 3)
        assert client.get("/network/events").status_code == 200
        assert client.get("/network/tally-parties").status_code == 200
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()
