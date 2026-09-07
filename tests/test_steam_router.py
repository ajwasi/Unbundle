from unittest.mock import AsyncMock, patch

from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_STEAM, STATUS_OK, Credential
from app.models.steam_game import SteamGame
from app.security import encrypt_json


def _connect_steam(db):
    db.add(Credential(source=SOURCE_STEAM, encrypted_payload=encrypt_json({"api_key": "k", "steamid64": "765..."}), status=STATUS_OK))
    db.commit()


def test_steam_page_requires_auth(client):
    resp = client.get("/steam", follow_redirects=False)
    assert resp.status_code == 303


def test_steam_page_shows_not_configured_prompt(authed_client):
    resp = authed_client.get("/steam")
    assert resp.status_code == 200
    assert "isn't connected" in resp.text


def test_steam_page_shows_games_and_stats(authed_client, db):
    _connect_steam(db)
    db.add(SteamGame(appid=220, name="Half-Life 2", playtime_forever_minutes=120, img_icon_url=""))
    db.commit()

    resp = authed_client.get("/steam")
    assert "Half-Life 2" in resp.text
    assert "1 game(s) owned" in resp.text
    assert "2.0 hour(s)" in resp.text


def test_steam_page_shows_never_redeemed_section(authed_client, db):
    _connect_steam(db)
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed Game - Steam", steam_app_id="220", steam_owned=False))
    db.commit()

    resp = authed_client.get("/steam")
    assert "Unredeemed Game - Steam" in resp.text
    assert "Some Bundle" in resp.text
    assert "Never Redeemed on Steam (1)" in resp.text
    assert 'href="https://www.humblebundle.com/downloads?key=GK1"' in resp.text
    assert 'target="_blank"' in resp.text


def test_steam_page_excludes_owned_entitlements_from_never_redeemed(authed_client, db):
    _connect_steam(db)
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Owned Game - Steam", steam_app_id="220", steam_owned=True))
    db.commit()

    resp = authed_client.get("/steam")
    assert "Owned Game - Steam" not in resp.text
    assert "Never Redeemed on Steam (0)" in resp.text


def test_steam_page_excludes_unmatched_entitlements_from_never_redeemed(authed_client, db):
    # steam_owned is None ("not yet checked") — must not be conflated with "confirmed absent".
    _connect_steam(db)
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Combo Key", steam_app_id=None, steam_owned=None))
    db.commit()

    resp = authed_client.get("/steam")
    assert "Combo Key" not in resp.text
    assert "Never Redeemed on Steam (0)" in resp.text


def test_refresh_steam_triggers_sync_and_rerenders(authed_client, db):
    _connect_steam(db)
    from app.connectors.steam_connector import SteamGameData

    games = [SteamGameData(appid=220, name="Half-Life 2", playtime_forever_minutes=60, img_icon_url="")]
    with patch("app.routers.steam.steam_sync.steam_connector.fetch_owned_games", new=AsyncMock(return_value=games)):
        resp = authed_client.post("/steam/refresh")
    assert resp.status_code == 200
    assert "Half-Life 2" in resp.text


def test_refresh_steam_when_not_connected_does_not_crash(authed_client):
    resp = authed_client.post("/steam/refresh")
    assert resp.status_code == 200


def test_refresh_steam_shows_error_message_on_failure(authed_client, db):
    _connect_steam(db)
    with patch("app.routers.steam.steam_sync.steam_connector.fetch_owned_games", new=AsyncMock(side_effect=ValueError("profile is private"))):
        resp = authed_client.post("/steam/refresh")
    assert resp.status_code == 200
    assert "profile is private" in resp.text
