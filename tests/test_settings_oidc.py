from unittest.mock import AsyncMock, patch

import httpx

from app.models.credential import SOURCE_OIDC, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json


def test_settings_page_shows_oidc_redirect_uri(authed_client):
    resp = authed_client.get("/settings")
    assert "/auth/oidc/callback" in resp.text


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
    assert 'class="badge badge-ok">Saved<' in resp.text
    cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one()
    assert cred.status == STATUS_OK
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["enabled"] is True
    assert payload["issuer"] == "https://auth.example.com"


def test_save_oidc_missing_fields_rejected(authed_client, db):
    resp = authed_client.post("/settings/oidc", data={"issuer": "", "client_id": "", "client_secret": "", "enabled": "true"})
    assert resp.status_code == 200
    assert "required" in resp.text.lower()
    assert 'class="badge badge-ok">Saved<' not in resp.text
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
