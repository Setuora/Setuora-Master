from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.routers.maintenance as maintenance_router
from app.database import Base, get_db
from app.main import app
from app.models import User
from tests.factories import authenticate_client


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def override_db(Session):
    def override_get_db():
        with Session() as db:
            yield db

    return override_get_db


def test_maintenance_exposes_backup_controls_but_no_browser_reset_or_restore(
    monkeypatch,
):
    engine, Session = make_session()
    with Session() as db:
        db.add_all(
            [
                User(
                    id=1,
                    username="admin",
                    password_hash="x",
                    role="admin",
                    active=True,
                ),
                User(
                    id=2,
                    username="root",
                    password_hash="x",
                    role="super_admin",
                    active=True,
                ),
            ]
        )
        db.commit()

    backup_status = SimpleNamespace(
        enabled=True,
        directory="/tmp/backups",
        interval_hours=24,
        retention_count=14,
        latest_backup=None,
        offsite_directory=None,
        offsite_latest_backup=None,
    )
    monkeypatch.setattr(maintenance_router, "backup_status", lambda: backup_status)
    monkeypatch.setattr(
        maintenance_router,
        "sqlite_database_path",
        lambda: "/tmp/setuora.db",
    )

    app.dependency_overrides[get_db] = override_db(Session)
    try:
        client = TestClient(
            app,
            follow_redirects=False,
            headers={"Origin": "http://testserver"},
        )
        authenticate_client(client, 1)
        admin_page = client.get("/maintenance")
        authenticate_client(client, 2)
        root_page = client.get("/maintenance")
        reset = client.post("/maintenance/reset")
        restore = client.post("/maintenance/restore-upload")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert admin_page.status_code == 200
    assert root_page.status_code == 200
    assert 'href="/maintenance/backup.db"' in admin_page.text
    assert 'action="/maintenance/backup-settings"' not in admin_page.text
    assert 'action="/maintenance/backup-settings"' in root_page.text
    assert 'action="/maintenance/reset"' not in root_page.text
    assert 'action="/maintenance/restore-upload"' not in root_page.text
    assert (
        "Browser-based database reset and restore are intentionally unavailable" in root_page.text
    )
    assert reset.status_code == 404
    assert restore.status_code == 404


def test_backup_settings_route_is_super_admin_only():
    engine, Session = make_session()
    with Session() as db:
        db.add_all(
            [
                User(
                    id=1,
                    username="admin",
                    password_hash="x",
                    role="admin",
                    active=True,
                ),
                User(
                    id=2,
                    username="root",
                    password_hash="x",
                    role="super_admin",
                    active=True,
                ),
            ]
        )
        db.commit()

    app.dependency_overrides[get_db] = override_db(Session)
    try:
        client = TestClient(
            app,
            follow_redirects=False,
            headers={"Origin": "http://testserver"},
        )
        payload = {
            "automatic_backups_enabled": "false",
            "backup_directory": "/tmp/backups",
            "backup_interval_hours": "24",
            "backup_retention_count": "14",
            "backup_offsite_directory": "",
        }
        authenticate_client(client, 1)
        admin_response = client.post(
            "/maintenance/backup-settings",
            data=payload,
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert admin_response.status_code == 403


def test_invalid_backup_settings_redirect_with_validation_error(monkeypatch):
    engine, Session = make_session()
    with Session() as db:
        db.add(
            User(
                id=1,
                username="root",
                password_hash="x",
                role="super_admin",
                active=True,
            )
        )
        db.commit()

    def reject_settings(**_values):
        raise ValueError("Backup folder is required.")

    monkeypatch.setattr(
        maintenance_router,
        "update_backup_settings",
        reject_settings,
    )
    app.dependency_overrides[get_db] = override_db(Session)
    try:
        client = TestClient(
            app,
            follow_redirects=False,
            headers={"Origin": "http://testserver"},
        )
        authenticate_client(client, 1)
        response = client.post(
            "/maintenance/backup-settings",
            data={
                "automatic_backups_enabled": "false",
                "backup_directory": "/invalid/test/path",
                "backup_interval_hours": "24",
                "backup_retention_count": "14",
                "backup_offsite_directory": "",
            },
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 303
    assert response.headers["location"] == ("/maintenance?error=Backup%20folder%20is%20required.")


def test_download_backup_failure_redirects_to_maintenance(monkeypatch):
    engine, Session = make_session()
    with Session() as db:
        db.add(
            User(
                id=1,
                username="root",
                password_hash="x",
                role="super_admin",
                active=True,
            )
        )
        db.commit()

    def fail_backup():
        raise RuntimeError("backup failed")

    monkeypatch.setattr(
        maintenance_router,
        "create_sqlite_backup",
        fail_backup,
    )
    app.dependency_overrides[get_db] = override_db(Session)
    try:
        client = TestClient(
            app,
            follow_redirects=False,
            headers={"Origin": "http://testserver"},
        )
        authenticate_client(client, 1)
        response = client.get("/maintenance/backup.db")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 303
    assert response.headers["location"] == "/maintenance?error=backup_failed"
