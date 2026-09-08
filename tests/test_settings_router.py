from unittest.mock import AsyncMock, patch

import httpx

from app.connectors.base import CredentialStatus
from app.models.credential import SOURCE_HUMBLE, SOURCE_OIDC, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json
from tests.factories import make_order


def test_settings_requires_auth(client):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303


def test_settings_page_shows_not_configured_by_default(authed_client):
    resp = authed_client.get("/settings")
    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()


def test_settings_page_shows_oidc_redirect_uri(authed_client):
    resp = authed_client.get("/settings")
    assert "/auth/oidc/callback" in resp.text


def test_save_humble_key_success_encrypts_and_writes_key_file(authed_client, db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.settings.settings.humble_cli_key_path", tmp_path / "key-file")
    ok_status = CredentialStatus(ok=True, message="Connected")
    with patch("app.routers.settings.HumbleConnector.check_credentials", new=AsyncMock(return_value=ok_status)):
        resp = authed_client.post("/settings/humble-key", data={"session_key": "real-cookie-value"})

    assert resp.status_code == 200
    assert "ok" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one()
    assert cred.status == STATUS_OK
    assert decrypt_json(cred.encrypted_payload) == {"session_key": "real-cookie-value"}
    assert (tmp_path / "key-file").read_text() == "real-cookie-value"


def test_save_humble_key_failure_stores_error_status(authed_client, db):
    bad_status = CredentialStatus(ok=False, message="Humble rejected this session key.")
    with patch("app.routers.settings.HumbleConnector.check_credentials", new=AsyncMock(return_value=bad_status)):
        resp = authed_client.post("/settings/humble-key", data={"session_key": "bad-cookie"})

    assert resp.status_code == 200
    cred = db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one()
    assert cred.status == STATUS_ERROR
    assert cred.last_error == "Humble rejected this session key."


def test_settings_page_hides_humble_disconnect_button_when_not_configured(authed_client):
    resp = authed_client.get("/settings")
    assert "/settings/humble/disconnect" not in resp.text


def test_settings_page_shows_humble_disconnect_button_when_configured(authed_client, db):
    db.add(Credential(source=SOURCE_HUMBLE, status=STATUS_OK, encrypted_payload=encrypt_json({"session_key": "x"})))
    db.commit()
    resp = authed_client.get("/settings")
    assert "/settings/humble/disconnect" in resp.text


def test_disconnect_humble_removes_credential_and_key_file(authed_client, db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.settings.settings.humble_cli_key_path", tmp_path / "key-file")
    (tmp_path / "key-file").write_text("real-cookie-value")
    db.add(Credential(source=SOURCE_HUMBLE, status=STATUS_OK, encrypted_payload=encrypt_json({"session_key": "real-cookie-value"})))
    db.commit()

    resp = authed_client.post("/settings/humble/disconnect")

    assert resp.status_code == 200
    assert "not configured" in resp.text.lower()
    assert db.query(Credential).filter(Credential.source == SOURCE_HUMBLE).one_or_none() is None
    assert not (tmp_path / "key-file").exists()


def test_disconnect_humble_is_a_noop_when_nothing_configured(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.settings.settings.humble_cli_key_path", tmp_path / "key-file")
    resp = authed_client.post("/settings/humble/disconnect")
    assert resp.status_code == 200


def _discovery_ok():
    return httpx.Response(
        200,
        json={"authorization_endpoint": "a", "token_endpoint": "b"},
        request=httpx.Request("GET", "https://x"),
    )


def test_save_oidc_enable_success(authed_client, db):
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_discovery_ok())):
        resp = authed_client.post(
            "/settings/oidc",
            data={"issuer": "https://auth.example.com", "client_id": "cid", "client_secret": "secret", "enabled": "true"},
        )
    assert resp.status_code == 200
    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one()
    assert cred.status == STATUS_OK
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["enabled"] is True
    assert payload["issuer"] == "https://auth.example.com"


def test_save_oidc_missing_fields_rejected(authed_client, db):
    resp = authed_client.post("/settings/oidc", data={"issuer": "", "client_id": "", "client_secret": "", "enabled": "true"})
    assert resp.status_code == 200
    assert "required" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one()
    assert cred.status == STATUS_ERROR


def test_save_oidc_bad_issuer_rejected(authed_client, db):
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = authed_client.post(
            "/settings/oidc",
            data={"issuer": "https://not-real.example.com", "client_id": "cid", "client_secret": "secret", "enabled": "true"},
        )
    assert "could not verify issuer" in resp.text.lower()
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_OIDC).one().encrypted_payload)
    assert payload["enabled"] is False


def test_save_oidc_disable_password_without_enabled_is_rejected(authed_client, db):
    resp = authed_client.post(
        "/settings/oidc",
        data={"issuer": "https://auth.example.com", "client_id": "cid", "client_secret": "secret", "disable_password": "true"},
    )
    assert "must be enabled and verified" in resp.text.lower()
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_OIDC).one().encrypted_payload)
    assert payload["disable_password"] is False
    assert payload["enabled"] is False


def test_save_oidc_disable_password_succeeds_when_enabled_and_verified(authed_client, db):
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_discovery_ok())):
        resp = authed_client.post(
            "/settings/oidc",
            data={
                "issuer": "https://auth.example.com",
                "client_id": "cid",
                "client_secret": "secret",
                "enabled": "true",
                "disable_password": "true",
            },
        )
    assert resp.status_code == 200
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_OIDC).one().encrypted_payload)
    assert payload["disable_password"] is True


def test_save_oidc_blank_secret_keeps_existing_one(authed_client, db):
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_discovery_ok())):
        authed_client.post(
            "/settings/oidc",
            data={"issuer": "https://auth.example.com", "client_id": "cid", "client_secret": "original-secret", "enabled": "true"},
        )
        resp = authed_client.post(
            "/settings/oidc",
            data={"issuer": "https://auth.example.com", "client_id": "cid", "client_secret": "", "enabled": "true"},
        )
    assert resp.status_code == 200
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_OIDC).one().encrypted_payload)
    assert payload["client_secret"] == "original-secret"


def test_settings_page_shows_backup_card(authed_client):
    resp = authed_client.get("/settings")
    assert "backup-card" in resp.text


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
