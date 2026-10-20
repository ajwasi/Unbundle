from unittest.mock import AsyncMock, patch

from app.connectors.types import CredentialStatus
from app.models.credential import SOURCE_NAVIDROME, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json


def test_settings_page_shows_navidrome_card(authed_client):
    resp = authed_client.get("/settings")
    assert "Navidrome" in resp.text
    assert 'name="username"' in resp.text


def test_save_navidrome_success_stores_credential(authed_client, db):
    with patch(
        "app.routers.settings.navidrome.navidrome_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected as alice")),
    ):
        resp = authed_client.post(
            "/settings/navidrome", data={"base_url": "https://nd.example.com", "username": "alice", "password": "secret"}
        )

    assert resp.status_code == 200
    assert "Connected as alice" in resp.text
    cred = db.query(Credential).filter(Credential.source == SOURCE_NAVIDROME).one()
    assert cred.status == STATUS_OK
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["username"] == "alice"
    assert payload["password"] == "secret"


def test_save_navidrome_missing_fields_rejected(authed_client, db):
    resp = authed_client.post("/settings/navidrome", data={"base_url": "", "username": "", "password": ""})
    assert "required" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_NAVIDROME).one()
    assert cred.status == STATUS_ERROR


def test_save_navidrome_blank_password_keeps_the_saved_one(authed_client, db):
    with patch(
        "app.routers.settings.navidrome.navidrome_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected")),
    ):
        authed_client.post("/settings/navidrome", data={"base_url": "https://nd.example.com", "username": "alice", "password": "secret"})
        authed_client.post("/settings/navidrome", data={"base_url": "https://nd.example.com", "username": "alice", "password": ""})

    cred = db.query(Credential).filter(Credential.source == SOURCE_NAVIDROME).one()
    assert decrypt_json(cred.encrypted_payload)["password"] == "secret"


def test_refresh_navidrome_reports_item_count(authed_client, db):
    with patch(
        "app.routers.settings.navidrome.navidrome_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected")),
    ):
        authed_client.post("/settings/navidrome", data={"base_url": "https://nd.example.com", "username": "alice", "password": "secret"})

    with patch("app.sync.navidrome_sync.navidrome_connector.fetch_albums", new=AsyncMock(return_value=[])):
        resp = authed_client.post("/settings/navidrome/refresh")

    assert resp.status_code == 200
    assert "Synced 0 album" in resp.text


def test_settings_page_hides_disconnect_button_when_not_configured(authed_client):
    resp = authed_client.get("/settings")
    assert "/settings/navidrome/disconnect" not in resp.text


def test_disconnect_navidrome_removes_credential(authed_client, db):
    db.add(Credential(source=SOURCE_NAVIDROME, status=STATUS_OK, encrypted_payload=None))
    db.commit()

    resp = authed_client.post("/settings/navidrome/disconnect")
    assert resp.status_code == 200
    assert db.query(Credential).filter(Credential.source == SOURCE_NAVIDROME).one_or_none() is None
    assert "not configured" in resp.text.lower()
