import asyncio
import os
import sqlite3
from contextlib import suppress
from types import SimpleNamespace

import app.services.backup as backup_service
import app.services.backup_worker as backup_worker
from app.config import get_settings
from app.services.backup import (
    create_scheduled_backup,
    create_sqlite_backup,
    sqlite_database_path,
    verify_sqlite_backup,
)


def test_backup_worker_start_replaces_finished_task(monkeypatch):
    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace())

        async def already_done():
            return None

        finished = asyncio.create_task(already_done())
        await finished
        setattr(app.state, backup_worker.WORKER_STATE_KEY, finished)

        async def replacement_loop():
            await asyncio.Event().wait()

        monkeypatch.setattr(
            backup_worker, "get_settings", lambda: SimpleNamespace(automatic_backups_enabled=True)
        )
        monkeypatch.setattr(backup_worker, "backup_worker_loop", replacement_loop)
        backup_worker.start_backup_worker(app)
        replacement = getattr(app.state, backup_worker.WORKER_STATE_KEY)

        assert replacement is not finished
        assert replacement.done() is False

        replacement.cancel()
        with suppress(asyncio.CancelledError):
            await replacement

    asyncio.run(scenario())


def test_create_sqlite_backup_uses_configured_database(tmp_path, monkeypatch):
    db_path = tmp_path / "setuora.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO sample (name) VALUES ('ok')")
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        backup_service, "get_settings", lambda: SimpleNamespace(database_url=f"sqlite:///{db_path}")
    )
    assert sqlite_database_path() == db_path.resolve()
    backup = create_sqlite_backup()
    assert backup.data.startswith(b"SQLite format 3")


def test_create_sqlite_backup_rejects_corrupt_backup(tmp_path):
    corrupt = tmp_path / "bad.db"
    corrupt.write_bytes(b"not sqlite")

    try:
        verify_sqlite_backup(corrupt)
    except RuntimeError as exc:
        assert "integrity" in str(exc).lower() or "database" in str(exc).lower()
    else:
        raise AssertionError("Expected corrupt backup verification to fail")


def test_scheduled_backup_verifies_copies_offsite_and_prunes(tmp_path, monkeypatch):
    db_path = tmp_path / "setuora.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    connection.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
    )
    connection.execute("INSERT INTO parent (id) VALUES (1)")
    connection.execute("INSERT INTO child (parent_id) VALUES (1)")
    connection.commit()
    connection.close()

    backup_dir = tmp_path / "backups"
    offsite_dir = tmp_path / "offsite"
    monkeypatch.setattr(
        backup_service,
        "get_settings",
        lambda: SimpleNamespace(
            database_url=f"sqlite:///{db_path}",
            backup_directory=str(backup_dir),
            backup_offsite_directory=str(offsite_dir),
            backup_retention_count=2,
        ),
    )

    first = create_scheduled_backup()
    second = create_scheduled_backup()
    third = create_scheduled_backup()

    assert first.path.exists() is False
    assert second.path.exists()
    assert third.path.exists()
    assert third.offsite_path is not None
    assert third.offsite_path.exists()
    verify_sqlite_backup(third.path)
    verify_sqlite_backup(third.offsite_path)
    assert len(list(backup_dir.glob("setuora-backup-*.db"))) == 2
    assert len(list(offsite_dir.glob("setuora-backup-*.db"))) == 2


def test_list_backup_files_includes_legacy_setu_prefix(tmp_path, monkeypatch):
    db_path = tmp_path / "setuora.db"
    db_path.write_bytes(b"")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    legacy = backup_dir / "setu-backup-20260702-163934-737317.db"
    current = backup_dir / "setuora-backup-20260709-153824-133945.db"
    other = backup_dir / "manual-copy.db"
    legacy.write_bytes(b"legacy")
    current.write_bytes(b"current")
    other.write_bytes(b"other")
    os.utime(legacy, (1, 1))
    os.utime(current, (2, 2))

    monkeypatch.setattr(
        backup_service,
        "get_settings",
        lambda: SimpleNamespace(
            database_url=f"sqlite:///{db_path}",
            backup_directory=str(backup_dir),
            backup_offsite_directory="",
        ),
    )

    assert backup_service.list_backup_files() == [current, legacy]


def test_update_backup_settings_persists_env_and_refreshes_runtime(tmp_path, monkeypatch):
    for key in backup_service.BACKUP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(backup_service, "ENV_FILE", env_file)
    get_settings.cache_clear()

    backup_dir = tmp_path / "backups with space"
    offsite_dir = tmp_path / "offsite"
    status = backup_service.update_backup_settings(
        enabled=False,
        backup_directory=str(backup_dir),
        interval_hours="6",
        retention_count="7",
        offsite_directory=str(offsite_dir),
    )

    assert status.enabled is False
    assert status.interval_hours == 6
    assert status.retention_count == 7
    assert backup_dir.exists()
    assert offsite_dir.exists()
    env_text = env_file.read_text(encoding="utf-8")
    assert "AUTOMATIC_BACKUPS_ENABLED=false" in env_text
    assert f'BACKUP_DIRECTORY="{backup_dir}"' in env_text
    assert "BACKUP_INTERVAL_HOURS=6" in env_text
    assert os.environ["BACKUP_RETENTION_COUNT"] == "7"
    assert get_settings().backup_retention_count == 7
    get_settings.cache_clear()
