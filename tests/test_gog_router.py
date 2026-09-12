from datetime import datetime, timedelta
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


def test_gog_page_shows_never_refreshed_with_no_games(authed_client, db):
    _connect_gog(db)
    resp = authed_client.get("/gog")
    assert "Never refreshed" in resp.text


def test_gog_page_shows_last_refreshed_time(authed_client, db):
    _connect_gog(db)
    fetched_at = datetime.utcnow() - timedelta(hours=3)
    db.add(GogGame(product_id=1, title="Shadowrun Returns", image_url="", fetched_at=fetched_at))
    db.commit()

    resp = authed_client.get("/gog")
    assert "Last refreshed" in resp.text
    assert "3 hours ago" in resp.text


def test_gog_page_shows_never_redeemed_section(authed_client, db):
    _connect_gog(db)
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed - GOG", gog_id="1", gog_owned=False))
    db.commit()

    resp = authed_client.get("/gog")
    assert "Unredeemed - GOG" in resp.text
    assert "Never Redeemed on GOG (1)" in resp.text
    assert 'href="https://www.humblebundle.com/downloads?key=GK1"' in resp.text


def test_gog_page_shows_name_matched_unredeemed_rows_too(authed_client, db):
    """gog_owned can be set via name-matching alone (see sync/gog_sync.py),
    with no gog_id ever populated — the page's own query must not require
    gog_id, or every name-matched row would be silently excluded here.
    """
    _connect_gog(db)
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Liberated", gog_id=None, gog_owned=False))
    db.commit()

    resp = authed_client.get("/gog")
    assert "Liberated" in resp.text
    assert "Never Redeemed on GOG (1)" in resp.text


def test_gog_page_flags_a_key_owned_under_a_different_gog_listing(authed_client, db):
    # gog_owned=False on the entitlement is a miss against its own recorded
    # gog_id (2) — but the same title (case-insensitive) is separately owned
    # under a *different* product_id (1), e.g. a re-release/edition.
    _connect_gog(db)
    db.add(GogGame(product_id=1, title="unredeemed - gog", image_url=""))
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed - GOG", gog_id="2", gog_owned=False))
    db.commit()

    resp = authed_client.get("/gog")
    assert "GOG (different listing)" in resp.text


def test_gog_page_flags_a_key_owned_on_steam(authed_client, db):
    from app.models.steam_game import SteamGame

    _connect_gog(db)
    db.add(SteamGame(appid=220, name="Unredeemed - GOG", playtime_forever_minutes=0, img_icon_url=""))
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed - GOG", gog_id=None, gog_owned=False))
    db.commit()

    resp = authed_client.get("/gog")
    assert 'class="badge badge-pending">Steam<' in resp.text


def test_gog_page_shows_no_ownership_hint_when_not_owned_anywhere(authed_client, db):
    _connect_gog(db)
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(BundleEntitlement(gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed - GOG", gog_id=None, gog_owned=False))
    db.commit()

    resp = authed_client.get("/gog")
    assert "badge-pending" not in resp.text


def test_gog_page_shows_expiration_date_and_highlights_expired_row(authed_client, db):
    import json
    from datetime import datetime, timedelta, timezone

    _connect_gog(db)
    past = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0).isoformat().replace("+00:00", "")
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed - GOG",
            gog_id=None, gog_owned=False, raw_json=json.dumps({"expiration_date": past}),
        )
    )
    db.commit()

    resp = authed_client.get("/gog")
    assert "(expired)" in resp.text
    assert 'class="row-expired"' in resp.text


def test_gog_page_shows_days_remaining_for_future_expiration(authed_client, db):
    import json
    from datetime import datetime, timedelta, timezone

    _connect_gog(db)
    future = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=0).isoformat().replace("+00:00", "")
    db.add(Bundle(gamekey="GK1", name="Some Bundle", raw_json="{}"))
    db.add(
        BundleEntitlement(
            gamekey="GK1", machine_name="m", keyindex=0, key_name="Unredeemed - GOG",
            gog_id=None, gog_owned=False, raw_json=json.dumps({"expiration_date": future}),
        )
    )
    db.commit()

    resp = authed_client.get("/gog")
    assert "in 9d" in resp.text or "in 10d" in resp.text
    assert 'class="row-expired"' not in resp.text


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
