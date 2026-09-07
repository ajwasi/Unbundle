from unittest.mock import AsyncMock, patch

from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_GOG, STATUS_OK, Credential
from app.models.gog_game import GogGame
from app.security import encrypt_json


def _connect_gog(db):
    db.add(Credential(source=SOURCE_GOG, encrypted_payload=encrypt_json({"refresh_token": "rt"}), status=STATUS_OK))
    db.commit()


def test_gog_page_requires_auth(client):
    resp = client.get("/gog", follow_redirects=False)
    assert resp.status_code == 303


def test_gog_page_shows_not_configured_prompt(authed_client):
    resp = authed_client.get("/gog")
    assert resp.status_code == 200
    assert "isn't connected" in resp.text


def test_gog_page_shows_games(authed_client, db):
    _connect_gog(db)
    db.add(GogGame(product_id=1, title="Shadowrun Returns", image_url=""))
    db.commit()

    resp = authed_client.get("/gog")
    assert "Shadowrun Returns" in resp.text
    assert "1 game(s) owned" in resp.text


def test_gog_page_shows_never_redeemed_section(authed_client, db):
    _connect_gog(db)
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed - GOG", gog_id="1", gog_owned=False))
    db.commit()

    resp = authed_client.get("/gog")
    assert "Unredeemed - GOG" in resp.text
    assert "Never Redeemed on GOG (1)" in resp.text
    assert 'href="https://www.humblebundle.com/downloads?key=GK1"' in resp.text


def test_refresh_gog_triggers_sync_and_rerenders(authed_client, db):
    _connect_gog(db)
    from app.connectors.gog_connector import GogGameData

    games = [GogGameData(product_id=1, title="Shadowrun Returns", image_url="")]
    with patch("app.routers.gog.gog_sync.gog_connector.refresh_access_token", new=AsyncMock(return_value={"access_token": "AT"})):
        with patch("app.routers.gog.gog_sync.gog_connector.fetch_owned_games", new=AsyncMock(return_value=games)):
            resp = authed_client.post("/gog/refresh")
    assert resp.status_code == 200
    assert "Shadowrun Returns" in resp.text


def test_refresh_gog_when_not_connected_does_not_crash(authed_client):
    resp = authed_client.post("/gog/refresh")
    assert resp.status_code == 200
