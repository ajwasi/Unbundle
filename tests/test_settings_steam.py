from unittest.mock import AsyncMock, patch

from app.connectors.base import CredentialStatus
from app.models.credential import SOURCE_STEAM, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json


def test_settings_page_shows_steam_card(authed_client):
    resp = authed_client.get("/settings")
    assert "Steam" in resp.text
    assert 'name="api_key"' in resp.text


def test_save_steam_success_resolves_and_stores(authed_client, db):
    with patch("app.routers.settings.steam_connector.resolve_steamid", new=AsyncMock(return_value="76561198010668763")):
        with patch("app.routers.settings.steam_connector.check_credentials", new=AsyncMock(return_value=CredentialStatus(ok=True, message="Connected as Tester"))):
            resp = authed_client.post("/settings/steam", data={"api_key": "real-key", "steamid": "myusername"})

    assert resp.status_code == 200
    assert "Connected as Tester" in resp.text
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one()
    assert cred.status == STATUS_OK
    payload = decrypt_json(cred.encrypted_payload)
    assert payload["api_key"] == "real-key"
    assert payload["steamid64"] == "76561198010668763"


def test_save_steam_missing_fields_rejected(authed_client, db):
    resp = authed_client.post("/settings/steam", data={"api_key": "", "steamid": ""})
    assert "required" in resp.text.lower()
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one()
    assert cred.status == STATUS_ERROR


def test_save_steam_private_profile_rejected(authed_client, db):
    with patch("app.routers.settings.steam_connector.resolve_steamid", new=AsyncMock(return_value="765...")):
        with patch("app.routers.settings.steam_connector.check_credentials", new=AsyncMock(return_value=CredentialStatus(ok=False, message="This Steam profile is private."))):
            resp = authed_client.post("/settings/steam", data={"api_key": "k", "steamid": "765..."})
    assert "private" in resp.text.lower()


def test_settings_page_hides_disconnect_button_when_not_configured(authed_client):
    resp = authed_client.get("/settings")
    assert "/settings/steam/disconnect" not in resp.text


def test_disconnect_steam_removes_credential(authed_client, db):
    db.add(Credential(source=SOURCE_STEAM, status=STATUS_OK, encrypted_payload=None))
    db.commit()

    resp = authed_client.post("/settings/steam/disconnect")
    assert resp.status_code == 200
    assert db.query(Credential).filter(Credential.source == SOURCE_STEAM).one_or_none() is None
    assert "not configured" in resp.text.lower()


def test_disconnect_steam_shows_button_only_when_configured(authed_client, db):
    db.add(Credential(source=SOURCE_STEAM, status=STATUS_OK))
    db.commit()
    resp = authed_client.get("/settings")
    assert "/settings/steam/disconnect" in resp.text


def test_disconnect_steam_is_a_noop_when_nothing_configured(authed_client, db):
    resp = authed_client.post("/settings/steam/disconnect")
    assert resp.status_code == 200


def test_save_steam_bad_vanity_name_rejected(authed_client, db):
    with patch("app.routers.settings.steam_connector.resolve_steamid", new=AsyncMock(side_effect=ValueError("Could not resolve Steam profile 'x'."))):
        resp = authed_client.post("/settings/steam", data={"api_key": "k", "steamid": "x"})
    assert "could not resolve" in resp.text.lower()


def test_save_steam_blank_api_key_keeps_existing(authed_client, db):
    with patch("app.routers.settings.steam_connector.resolve_steamid", new=AsyncMock(return_value="765...")):
        with patch("app.routers.settings.steam_connector.check_credentials", new=AsyncMock(return_value=CredentialStatus(ok=True, message="ok"))):
            authed_client.post("/settings/steam", data={"api_key": "original-key", "steamid": "765..."})
            resp = authed_client.post("/settings/steam", data={"api_key": "", "steamid": "765..."})

    assert resp.status_code == 200
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_STEAM).one().encrypted_payload)
    assert payload["api_key"] == "original-key"


def test_save_steam_blank_steamid_keeps_existing_resolved_id(authed_client, db):
    with patch("app.routers.settings.steam_connector.resolve_steamid", new=AsyncMock(return_value="76561198010668763")):
        with patch("app.routers.settings.steam_connector.check_credentials", new=AsyncMock(return_value=CredentialStatus(ok=True, message="ok"))):
            authed_client.post("/settings/steam", data={"api_key": "k", "steamid": "myvanity"})
            resp = authed_client.post("/settings/steam", data={"api_key": "", "steamid": ""})

    assert resp.status_code == 200
    payload = decrypt_json(db.query(Credential).filter(Credential.source == SOURCE_STEAM).one().encrypted_payload)
    assert payload["steamid64"] == "76561198010668763"
