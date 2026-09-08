import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import backup
from app.db import Base, engine
from app.models.backup_settings import BackupSettings
from app.models.credential import SOURCE_HUMBLE, Credential
from tests.factories import make_order


def test_create_backup_produces_openable_file_with_current_data(db, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(name="Backed Up Bundle"))
    dest = backup.create_backup(db)

    assert dest.exists()
    conn = sqlite3.connect(dest)
    try:
        rows = conn.execute("SELECT gamekey FROM bundle").fetchall()
    finally:
        conn.close()
    assert ("GK1",) in rows


def test_create_backup_updates_last_backup_at(db):
    cfg_before = backup.get_or_create_backup_settings(db)
    assert cfg_before.last_backup_at is None

    backup.create_backup(db)

    cfg_after = backup.get_or_create_backup_settings(db)
    assert cfg_after.last_backup_at is not None


def test_enforce_retention_deletes_oldest_beyond_count(db):
    backup.backups_dir().mkdir(parents=True, exist_ok=True)
    names = ["humble-20260101-000000.db", "humble-20260102-000000.db", "humble-20260103-000000.db"]
    for name in names:
        (backup.backups_dir() / name).write_bytes(b"x")

    backup._enforce_retention(2)

    remaining = sorted(p.name for p in backup.backups_dir().glob("humble-*.db"))
    assert remaining == names[1:]


def test_delete_backup_removes_a_real_file(db):
    backup.backups_dir().mkdir(parents=True, exist_ok=True)
    (backup.backups_dir() / "humble-20260101-000000.db").write_bytes(b"x")

    assert backup.delete_backup("humble-20260101-000000.db") is True
    assert backup.list_backups() == []


def test_delete_backup_rejects_unknown_filename(db):
    backup.backups_dir().mkdir(parents=True, exist_ok=True)
    (backup.backups_dir() / "humble-20260101-000000.db").write_bytes(b"x")

    assert backup.delete_backup("../../etc/passwd") is False
    assert backup.delete_backup("humble-20260102-000000.db") is False
    assert len(backup.list_backups()) == 1


def test_list_backups_returns_newest_first(db):
    backup.backups_dir().mkdir(parents=True, exist_ok=True)
    (backup.backups_dir() / "humble-20260101-000000.db").write_bytes(b"x")
    (backup.backups_dir() / "humble-20260103-000000.db").write_bytes(b"xyz")

    rows = backup.list_backups()

    assert [r["name"] for r in rows] == ["humble-20260103-000000.db", "humble-20260101-000000.db"]
    assert rows[1]["size_bytes"] == 1


def _cfg(**overrides) -> BackupSettings:
    defaults = {"id": 1, "enabled": True, "daily_time_utc": "03:00", "retention_count": 7, "last_backup_at": None}
    defaults.update(overrides)
    return BackupSettings(**defaults)


def test_is_due_false_when_disabled():
    cfg = _cfg(enabled=False, last_backup_at=None)
    assert not backup._is_due(cfg, datetime(2026, 1, 2, 5, 0))


def test_is_due_true_when_never_backed_up():
    cfg = _cfg(last_backup_at=None)
    assert backup._is_due(cfg, datetime(2026, 1, 2, 5, 0))


def test_is_due_false_when_already_run_after_todays_target():
    cfg = _cfg(daily_time_utc="03:00", last_backup_at=datetime(2026, 1, 2, 3, 30))
    assert not backup._is_due(cfg, datetime(2026, 1, 2, 5, 0))


def test_is_due_true_when_last_run_was_before_todays_target():
    cfg = _cfg(daily_time_utc="03:00", last_backup_at=datetime(2026, 1, 1, 3, 30))
    assert backup._is_due(cfg, datetime(2026, 1, 2, 5, 0))


def test_is_due_false_when_now_is_before_todays_target_and_yesterdays_already_ran():
    cfg = _cfg(daily_time_utc="03:00", last_backup_at=datetime(2026, 1, 1, 3, 0))
    assert not backup._is_due(cfg, datetime(2026, 1, 2, 1, 0))


def test_is_due_catches_up_once_after_extended_downtime():
    cfg = _cfg(daily_time_utc="03:00", last_backup_at=datetime(2026, 1, 1, 3, 0))
    assert backup._is_due(cfg, datetime(2026, 1, 5, 12, 0))


def test_validate_backup_file_rejects_non_sqlite_file(tmp_path):
    fake = tmp_path / "not-a-db.db"
    fake.write_text("just some text")
    with pytest.raises(backup.InvalidBackupFile, match="isn't a SQLite database"):
        backup._validate_backup_file(fake)


def test_validate_backup_file_rejects_sqlite_file_missing_expected_tables(tmp_path):
    empty_db = tmp_path / "empty.db"
    conn = sqlite3.connect(empty_db)
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(backup.InvalidBackupFile, match="missing table"):
        backup._validate_backup_file(empty_db)


def _build_alternate_backup(tmp_path: Path, gamekey: str) -> Path:
    """A standalone, valid Humble Tracker database with different data than
    whatever's currently live — built directly against Base.metadata rather
    than through the app's own engine, so it's independent of the live DB.
    """
    alt_path = tmp_path / "alternate.db"
    alt_engine = create_engine(f"sqlite:///{alt_path}")
    Base.metadata.create_all(alt_engine)
    AltSession = sessionmaker(bind=alt_engine)
    session = AltSession()
    try:
        session.add(Credential(source=SOURCE_HUMBLE, status="ok"))
        from app.models.bundle import Bundle

        session.add(Bundle(gamekey=gamekey, name="Restored Bundle", raw_json="{}", fetched_at=datetime.utcnow()))
        session.commit()
    finally:
        session.close()
    alt_engine.dispose()
    return alt_path


def test_restore_backup_replaces_live_data(db, make_bundle, tmp_path):
    make_bundle(gamekey="ORIGINAL", order=make_order(name="Original Bundle"))
    alt_backup = _build_alternate_backup(tmp_path, gamekey="RESTORED")

    with patch("app.backup._upgrade_to_head"):
        backup.restore_backup(db, alt_backup)

    conn = sqlite3.connect(backup.db_file_path())
    try:
        gamekeys = {row[0] for row in conn.execute("SELECT gamekey FROM bundle")}
    finally:
        conn.close()
    assert gamekeys == {"RESTORED"}


def test_restore_backup_creates_safety_backup_of_pre_restore_state(db, make_bundle, tmp_path):
    make_bundle(gamekey="BEFORE_RESTORE", order=make_order(name="Pre-restore Bundle"))
    alt_backup = _build_alternate_backup(tmp_path, gamekey="AFTER_RESTORE")

    with patch("app.backup._upgrade_to_head"):
        backup.restore_backup(db, alt_backup)

    safety_backups = backup.list_backups()
    assert len(safety_backups) == 1
    conn = sqlite3.connect(backup.backups_dir() / safety_backups[0]["name"])
    try:
        gamekeys = {row[0] for row in conn.execute("SELECT gamekey FROM bundle")}
    finally:
        conn.close()
    assert gamekeys == {"BEFORE_RESTORE"}


def test_restore_backup_rejects_invalid_file_without_touching_live_db(db, make_bundle, tmp_path):
    make_bundle(gamekey="UNTOUCHED", order=make_order(name="Untouched Bundle"))
    bad_file = tmp_path / "bad.db"
    bad_file.write_text("not a database")

    with pytest.raises(backup.InvalidBackupFile):
        backup.restore_backup(db, bad_file)

    conn = sqlite3.connect(backup.db_file_path())
    try:
        gamekeys = {row[0] for row in conn.execute("SELECT gamekey FROM bundle")}
    finally:
        conn.close()
    assert gamekeys == {"UNTOUCHED"}
    assert backup.list_backups() == []
