from unittest.mock import AsyncMock, patch

from app.connectors.types import CredentialStatus
from app.models.credential import SOURCE_HUMBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json


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
