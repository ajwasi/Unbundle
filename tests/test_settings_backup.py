from tests.factories import make_order


def test_settings_page_shows_backup_card(authed_client):
    resp = authed_client.get("/settings")
    assert "backup-card" in resp.text


def test_settings_page_shows_backup_disabled_message_when_not_supported(authed_client, monkeypatch):
    monkeypatch.setattr("app.backup.backups_supported", lambda: False)
    resp = authed_client.get("/settings")
    assert "aren't available on this database backend" in resp.text
    assert 'name="daily_time_utc"' not in resp.text


def test_run_backup_now_returns_clean_message_instead_of_500_when_not_supported(authed_client, monkeypatch):
    # The route the plan called out as load-bearing: create_backup() ->
    # db_file_path() raises RuntimeError, which this route's own
    # `except OSError` does not catch — without the early-return guard this
    # would 500 instead of degrading cleanly.
    monkeypatch.setattr("app.backup.backups_supported", lambda: False)
    resp = authed_client.post("/settings/backups/run")
    assert resp.status_code == 200
    assert "aren't available on this database backend" in resp.text


def test_save_backup_config_does_not_persist_when_not_supported(authed_client, db, monkeypatch):
    from app import backup

    monkeypatch.setattr("app.backup.backups_supported", lambda: False)
    authed_client.post(
        "/settings/backups/config", data={"enabled": "true", "daily_time_utc": "04:30", "retention_count": "3"}
    )
    cfg = backup.get_or_create_backup_settings(db)
    assert cfg.enabled is False  # unchanged default, save was never attempted


def test_save_backup_config_persists_valid_settings(authed_client, db):
    from app import backup

    resp = authed_client.post(
        "/settings/backups/config",
        data={"enabled": "true", "daily_time_utc": "04:30", "retention_count": "3"},
    )
    assert resp.status_code == 200
    cfg = backup.get_or_create_backup_settings(db)
    assert cfg.enabled is True
    assert cfg.daily_time_utc == "04:30"
    assert cfg.retention_count == 3


def test_save_backup_config_rejects_malformed_time(authed_client, db):
    from app import backup

    resp = authed_client.post(
        "/settings/backups/config",
        data={"enabled": "true", "daily_time_utc": "25:99", "retention_count": "3"},
    )
    assert "24-hour" in resp.text
    cfg = backup.get_or_create_backup_settings(db)
    assert cfg.daily_time_utc == "03:00"  # unchanged default


def test_save_backup_config_rejects_zero_retention(authed_client):
    resp = authed_client.post(
        "/settings/backups/config",
        data={"enabled": "true", "daily_time_utc": "03:00", "retention_count": "0"},
    )
    assert "at least 1" in resp.text.lower()


def test_run_backup_now_creates_downloadable_file(authed_client, make_bundle):
    from app import backup

    make_bundle(gamekey="GK1")
    resp = authed_client.post("/settings/backups/run")
    assert resp.status_code == 200
    backups = backup.list_backups()
    assert len(backups) == 1
    assert f"/settings/backups/{backups[0]['name']}/download" in resp.text


def test_download_backup_returns_file_contents(authed_client, db):
    from app import backup

    dest = backup.create_backup(db)
    resp = authed_client.get(f"/settings/backups/{dest.name}/download")
    assert resp.status_code == 200
    assert resp.content == dest.read_bytes()


def test_download_backup_rejects_unknown_filename(authed_client):
    resp = authed_client.get("/settings/backups/does-not-exist.db/download")
    assert resp.status_code == 404


def test_delete_backup_removes_it_and_it_stops_being_downloadable(authed_client, db):
    from app import backup

    dest = backup.create_backup(db)
    resp = authed_client.post(f"/settings/backups/{dest.name}/delete")
    assert resp.status_code == 200
    assert dest.name not in resp.text
    assert backup.list_backups() == []

    resp = authed_client.get(f"/settings/backups/{dest.name}/download")
    assert resp.status_code == 404


def test_delete_backup_rejects_unknown_filename(authed_client):
    resp = authed_client.post("/settings/backups/does-not-exist.db/delete")
    assert resp.status_code == 200
    assert "not found" in resp.text.lower()


def test_restore_backup_route_replaces_live_data(authed_client, db, make_bundle):
    import sqlite3
    from unittest.mock import patch

    from app import backup
    from app.models.bundle import Bundle

    make_bundle(gamekey="FIRST", order=make_order(name="First Bundle"))
    backup.create_backup(db)
    first_backup = backup.list_backups()[0]
    first_backup_bytes = (backup.backups_dir() / first_backup["name"]).read_bytes()

    db.query(Bundle).delete()
    db.commit()
    make_bundle(gamekey="SECOND", order=make_order(name="Second Bundle"))

    with patch("app.backup._upgrade_to_head"):
        resp = authed_client.post(
            "/settings/backups/restore",
            files={"backup_file": (first_backup["name"], first_backup_bytes, "application/octet-stream")},
        )
    assert resp.status_code == 200

    conn = sqlite3.connect(backup.db_file_path())
    try:
        gamekeys = {row[0] for row in conn.execute("SELECT gamekey FROM bundle")}
    finally:
        conn.close()
    assert gamekeys == {"FIRST"}


def test_restore_backup_route_rejects_invalid_upload(authed_client, db, make_bundle):
    import sqlite3

    from app import backup

    make_bundle(gamekey="UNTOUCHED")

    resp = authed_client.post(
        "/settings/backups/restore",
        files={"backup_file": ("not-a-backup.db", b"not a real database", "application/octet-stream")},
    )
    assert resp.status_code == 200
    assert "isn&#39;t a SQLite database" in resp.text or "isn't a SQLite database" in resp.text

    conn = sqlite3.connect(backup.db_file_path())
    try:
        gamekeys = {row[0] for row in conn.execute("SELECT gamekey FROM bundle")}
    finally:
        conn.close()
    assert gamekeys == {"UNTOUCHED"}
