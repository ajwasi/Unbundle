from unittest.mock import AsyncMock, patch

import httpx

from app.connectors.base import CredentialStatus
from app.models.credential import SOURCE_HUMBLE, SOURCE_OIDC, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json


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
