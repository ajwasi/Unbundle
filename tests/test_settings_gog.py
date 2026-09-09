from unittest.mock import AsyncMock, patch

from app.connectors.gog_connector import GogAuthError
from app.models.credential import SOURCE_GOG, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json


def test_settings_page_shows_gog_card_with_login_link(authed_client):
    resp = authed_client.get("/settings")
    assert "GOG" in resp.text
    assert 'href="https://auth.gog.com/auth' in resp.text
    assert 'name="pasted_code"' in resp.text


def test_settings_page_shows_demo_shortcut_instead_of_real_login_link_in_demo_mode(authed_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "demo_mode", True)
    resp = authed_client.get("/settings")
    assert "auth.gog.com/auth" not in resp.text
    assert "Connect with demo data" in resp.text


def test_save_gog_success_with_bare_code(authed_client, db):
    with patch("app.routers.settings.gog_connector.exchange_code", new=AsyncMock(return_value={"access_token": "AT", "refresh_token": "RT"})) as mock_exchange:
        resp = authed_client.post("/settings/gog", data={"pasted_code": "abc123"})
    assert resp.status_code == 200
    mock_exchange.assert_awaited_once_with("abc123")
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one()
    assert cred.status == STATUS_OK
    assert decrypt_json(cred.encrypted_payload)["refresh_token"] == "RT"


def test_save_gog_success_extracts_code_from_pasted_full_url(authed_client, db):
    pasted = "https://embed.gog.com/on_login_success?origin=client&code=xyz789"
    with patch("app.routers.settings.gog_connector.exchange_code", new=AsyncMock(return_value={"access_token": "AT", "refresh_token": "RT"})) as mock_exchange:
        resp = authed_client.post("/settings/gog", data={"pasted_code": pasted})
    assert resp.status_code == 200
    mock_exchange.assert_awaited_once_with("xyz789")


def test_save_gog_empty_input_rejected(authed_client, db):
    resp = authed_client.post("/settings/gog", data={"pasted_code": ""})
    assert "paste" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one()
    assert cred.status == STATUS_ERROR


def test_save_gog_expired_code_shows_clear_error(authed_client, db):
    with patch("app.routers.settings.gog_connector.exchange_code", new=AsyncMock(side_effect=GogAuthError("GOG rejected the code (HTTP 400) — it may have expired. Log in again and paste a fresh one right away."))):
        resp = authed_client.post("/settings/gog", data={"pasted_code": "expired-code"})
    assert "may have expired" in resp.text
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one()
    assert cred.status == STATUS_ERROR


def test_save_gog_shows_reconnect_button_after_error(authed_client, db):
    db.add(Credential(source=SOURCE_GOG, status=STATUS_ERROR, last_error="it may have expired"))
    db.commit()

    resp = authed_client.get("/settings")
    assert "it may have expired" in resp.text


def test_settings_page_hides_disconnect_button_when_not_configured(authed_client):
    resp = authed_client.get("/settings")
    assert "/settings/gog/disconnect" not in resp.text


def test_disconnect_gog_removes_credential(authed_client, db):
    db.add(Credential(source=SOURCE_GOG, status=STATUS_OK, encrypted_payload=None))
    db.commit()

    resp = authed_client.post("/settings/gog/disconnect")
    assert resp.status_code == 200
    assert db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none() is None
    assert "not configured" in resp.text.lower()


def test_disconnect_gog_shows_button_only_when_configured(authed_client, db):
    db.add(Credential(source=SOURCE_GOG, status=STATUS_OK))
    db.commit()
    resp = authed_client.get("/settings")
    assert "/settings/gog/disconnect" in resp.text
