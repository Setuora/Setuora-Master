from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import User
from app.services.access_control import (
    ROLE_COLUMNS,
    config_from_form,
    configured_role_has_access,
    get_role_access_config,
    role_access_sections,
    save_role_access_config,
)
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


def test_role_access_catalog_contains_only_master_roles_and_capabilities():
    sections = role_access_sections()

    assert [role.key for role in ROLE_COLUMNS] == [
        "super_admin",
        "admin",
        "directors",
    ]
    assert [section.title for section in sections] == [
        "Master administration",
        "Backup access",
    ]
    assert [
        row.key
        for section in sections
        for row in section.rows
    ] == [
        "settings_edit",
        "tally_check_edit",
        "users_manage",
        "role_access_edit",
        "backup_data",
        "backup_download",
    ]
    assert all(
        len(row.cells) == len(ROLE_COLUMNS)
        for section in sections
        for row in section.rows
    )


def test_master_role_defaults_and_multi_role_union():
    engine, Session = make_session()
    with Session() as db:
        config = get_role_access_config(db)
    engine.dispose()

    assert configured_role_has_access(config, "admin", "settings_edit")
    assert configured_role_has_access(config, "admin", "backup_download")
    assert not configured_role_has_access(config, "admin", "role_access_edit")
    assert not configured_role_has_access(config, "directors", "settings_edit")
    assert configured_role_has_access(
        config,
        "directors,admin",
        "settings_edit",
    )
    assert set(config) == {
        "settings_edit",
        "tally_check_edit",
        "users_manage",
        "role_access_edit",
        "backup_data",
        "backup_download",
    }


def test_partial_save_ignores_unknown_roles_keys_and_invalid_values():
    engine, Session = make_session()
    with Session() as db:
        save_role_access_config(
            db,
            {
                "settings_edit": {
                    "admin": "no",
                    "directors": "no",
                }
            },
        )
        submitted = config_from_form(
            [
                ("access__settings_edit__admin", "edit"),
                ("access__settings_edit__directors", "invalid"),
                ("access__settings_edit__purchase", "edit"),
                ("access__unknown__admin", "edit"),
                ("access__role_access_edit__admin", "edit"),
                ("access__settings_edit__super_admin", "no"),
            ]
        )
        save_role_access_config(db, submitted)
        saved = get_role_access_config(db)
    engine.dispose()

    assert submitted == {
        "settings_edit": {"admin": "edit"},
        "role_access_edit": {"admin": "edit"},
    }
    assert saved["settings_edit"] == {
        "super_admin": "edit",
        "admin": "edit",
        "directors": "no",
    }
    assert saved["role_access_edit"] == {
        "super_admin": "edit",
        "admin": "no",
        "directors": "no",
    }


def test_role_access_page_is_super_admin_only_and_has_master_navigation():
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
                    username="director",
                    password_hash="x",
                    role="directors",
                    active=True,
                ),
                User(
                    id=3,
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
        authenticate_client(client, 3)
        root_response = client.get("/settings/access")
        authenticate_client(client, 1)
        admin_response = client.get("/settings/access")
        authenticate_client(client, 2)
        director_response = client.get("/settings/access")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert root_response.status_code == 200
    assert "Role Access" in root_response.text
    assert "Master administration" in root_response.text
    assert "Backup access" in root_response.text
    assert 'name="access__settings_edit__admin"' in root_response.text
    assert 'name="access__backup_download__directors"' in root_response.text
    assert ">Purchase</summary>" not in root_response.text
    assert ">Sales</summary>" not in root_response.text
    assert "/batches" not in root_response.text
    assert "/products" not in root_response.text
    assert "/serials" not in root_response.text
    assert 'href="/network/transfers"' in root_response.text
    assert 'href="/network/reports"' in root_response.text
    assert 'href="/franchises"' in root_response.text
    assert admin_response.status_code == 403
    assert director_response.status_code == 403


def test_saved_master_permissions_protect_settings_and_maintenance_routes():
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
        authenticate_client(client, 1)
        assert client.get("/settings").status_code == 200
        assert client.get("/maintenance").status_code == 200

        authenticate_client(client, 2)
        save_response = client.post(
            "/settings/access",
            data={
                "access__settings_edit__admin": "no",
                "access__backup_data__admin": "no",
            },
        )
        authenticate_client(client, 1)
        settings_after = client.get("/settings")
        maintenance_after = client.get("/maintenance")
    finally:
        app.dependency_overrides.clear()

    with Session() as db:
        saved = get_role_access_config(db)
    engine.dispose()

    assert save_response.status_code == 303
    assert settings_after.status_code == 403
    assert maintenance_after.status_code == 403
    assert saved["settings_edit"]["admin"] == "no"
    assert saved["backup_data"]["admin"] == "no"
