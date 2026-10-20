from unittest.mock import AsyncMock, patch

from app.connectors.types import CredentialStatus
from app.models.credential import SOURCE_PLEX, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json


def test_settings_page_shows_plex_card(authed_client):
    resp = authed_client.get("/settings")
    assert "Plex" in resp.text
    assert 'name="base_url"' in resp.text
    assert 'name="library_id"' in resp.text


def test_save_plex_success_stores_credential(authed_client, db):
    with patch(
        "app.routers.settings.plex.plex_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected — libraries: Audiobooks (key 3, type artist)")),
    ):
        resp = authed_client.post(
            "/settings/plex", data={"base_url": "http://plex.example.com:32400", "token": "tok", "library_id": ""}
        )

    assert resp.status_code == 200
    assert "Connected" in resp.text
    cred = db.query(Credential).filter(Credential.source == SOURCE_PLEX).one()
    assert cred.status == STATUS_OK
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["base_url"] == "http://plex.example.com:32400"
    assert payload["token"] == "tok"


def test_save_plex_missing_fields_rejected(authed_client, db):
    resp = authed_client.post("/settings/plex", data={"base_url": "", "token": ""})
    assert "required" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_PLEX).one()
    assert cred.status == STATUS_ERROR


def test_save_plex_blank_token_keeps_the_saved_one(authed_client, db):
    with patch(
        "app.routers.settings.plex.plex_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected")),
    ):
        authed_client.post("/settings/plex", data={"base_url": "http://plex.example.com:32400", "token": "tok", "library_id": "3"})
        authed_client.post("/settings/plex", data={"base_url": "http://plex.example.com:32400", "token": "", "library_id": "3"})

    cred = db.query(Credential).filter(Credential.source == SOURCE_PLEX).one()
    assert decrypt_json(cred.encrypted_payload)["token"] == "tok"


def test_refresh_plex_reports_item_count(authed_client, db):
    with patch(
        "app.routers.settings.plex.plex_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected")),
    ):
        authed_client.post("/settings/plex", data={"base_url": "http://plex.example.com:32400", "token": "tok", "library_id": "3"})

    with patch("app.sync.plex_sync.plex_connector.fetch_audiobook_items", new=AsyncMock(return_value=[])):
        resp = authed_client.post("/settings/plex/refresh")

    assert resp.status_code == 200
    assert "Synced 0 item" in resp.text


def test_refresh_plex_surfaces_the_missing_library_id_error(authed_client, db):
    with patch(
        "app.routers.settings.plex.plex_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected")),
    ):
        authed_client.post("/settings/plex", data={"base_url": "http://plex.example.com:32400", "token": "tok", "library_id": ""})

    resp = authed_client.post("/settings/plex/refresh")
    assert "library section ID is required" in resp.text


def test_settings_page_hides_disconnect_button_when_not_configured(authed_client):
    resp = authed_client.get("/settings")
    assert "/settings/plex/disconnect" not in resp.text


def test_disconnect_plex_removes_credential(authed_client, db):
    db.add(Credential(source=SOURCE_PLEX, status=STATUS_OK, encrypted_payload=None))
    db.commit()

    resp = authed_client.post("/settings/plex/disconnect")
    assert resp.status_code == 200
    assert db.query(Credential).filter(Credential.source == SOURCE_PLEX).one_or_none() is None
    assert "not configured" in resp.text.lower()
