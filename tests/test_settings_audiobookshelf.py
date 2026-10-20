from unittest.mock import AsyncMock, patch

from app.connectors.types import CredentialStatus
from app.models.credential import SOURCE_AUDIOBOOKSHELF, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json


def test_settings_page_shows_audiobookshelf_card(authed_client):
    resp = authed_client.get("/settings")
    assert "AudiobookShelf" in resp.text
    assert 'name="base_url"' in resp.text


def test_save_audiobookshelf_success_stores_credential(authed_client, db):
    with patch(
        "app.routers.settings.audiobookshelf.audiobookshelf_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected — audiobook library: Audiobooks")),
    ):
        resp = authed_client.post(
            "/settings/audiobookshelf", data={"base_url": "https://abs.example.com", "api_token": "tok", "library_id": ""}
        )

    assert resp.status_code == 200
    assert "Connected" in resp.text
    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIOBOOKSHELF).one()
    assert cred.status == STATUS_OK
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["base_url"] == "https://abs.example.com"
    assert payload["api_token"] == "tok"


def test_save_audiobookshelf_missing_fields_rejected(authed_client, db):
    resp = authed_client.post("/settings/audiobookshelf", data={"base_url": "", "api_token": ""})
    assert "required" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIOBOOKSHELF).one()
    assert cred.status == STATUS_ERROR


def test_save_audiobookshelf_rejected_token_surfaces_message(authed_client, db):
    with patch(
        "app.routers.settings.audiobookshelf.audiobookshelf_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=False, message="AudiobookShelf rejected this API token.")),
    ):
        resp = authed_client.post(
            "/settings/audiobookshelf", data={"base_url": "https://abs.example.com", "api_token": "bad", "library_id": ""}
        )
    assert "rejected" in resp.text.lower()


def test_save_audiobookshelf_blank_token_keeps_the_saved_one(authed_client, db):
    with patch(
        "app.routers.settings.audiobookshelf.audiobookshelf_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected")),
    ):
        authed_client.post("/settings/audiobookshelf", data={"base_url": "https://abs.example.com", "api_token": "tok", "library_id": ""})
        authed_client.post("/settings/audiobookshelf", data={"base_url": "https://abs.example.com", "api_token": "", "library_id": ""})

    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIOBOOKSHELF).one()
    assert decrypt_json(cred.encrypted_payload)["api_token"] == "tok"


def test_refresh_audiobookshelf_reports_item_count(authed_client, db):
    with patch(
        "app.routers.settings.audiobookshelf.audiobookshelf_connector.check_credentials",
        new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected")),
    ):
        authed_client.post("/settings/audiobookshelf", data={"base_url": "https://abs.example.com", "api_token": "tok", "library_id": ""})

    with patch("app.sync.audiobookshelf_sync.audiobookshelf_connector.fetch_library_items", new=AsyncMock(return_value=[])):
        resp = authed_client.post("/settings/audiobookshelf/refresh")

    assert resp.status_code == 200
    assert "Synced 0 item" in resp.text


def test_settings_page_hides_disconnect_button_when_not_configured(authed_client):
    resp = authed_client.get("/settings")
    assert "/settings/audiobookshelf/disconnect" not in resp.text


def test_disconnect_audiobookshelf_removes_credential(authed_client, db):
    db.add(Credential(source=SOURCE_AUDIOBOOKSHELF, status=STATUS_OK, encrypted_payload=None))
    db.commit()

    resp = authed_client.post("/settings/audiobookshelf/disconnect")
    assert resp.status_code == 200
    assert db.query(Credential).filter(Credential.source == SOURCE_AUDIOBOOKSHELF).one_or_none() is None
    assert "not configured" in resp.text.lower()
