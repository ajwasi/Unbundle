from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.steam_connector import SteamGameData
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_STEAM, STATUS_ERROR, STATUS_OK, Credential
from app.models.steam_game import SteamGame
from app.security import encrypt_json
from app.sync import steam_sync


def _connect_steam(db):
    db.add(Credential(source=SOURCE_STEAM, encrypted_payload=encrypt_json({"api_key": "k", "steamid64": "765..."}), status=STATUS_OK))
    db.commit()


def _seed_bundle_and_entitlement(db, gamekey="GK1", **overrides):
    from app.models.bundle import Bundle

    db.add(Bundle(gamekey=gamekey, name="B", raw_json="{}"))
    ent = BundleEntitlement(gamekey=gamekey, machine_name="m", keyindex=0, key_name="Some Game - Steam")
    for k, v in overrides.items():
        setattr(ent, k, v)
    db.add(ent)
    db.commit()
    db.refresh(ent)
    return ent


@pytest.mark.asyncio
async def test_refresh_steam_library_raises_when_not_connected(db):
    with pytest.raises(steam_sync.NotConnectedError):
        await steam_sync.refresh_steam_library(db)


@pytest.mark.asyncio
async def test_refresh_steam_library_upserts_games(db):
    _connect_steam(db)
    games = [SteamGameData(appid=220, name="Half-Life 2", playtime_forever_minutes=60, img_icon_url="")]
    with patch("app.sync.steam_sync.steam_connector.fetch_owned_games", new=AsyncMock(return_value=games)):
        count = await steam_sync.refresh_steam_library(db)

    assert count == 1
    saved = db.get(SteamGame, 220)
    assert saved.name == "Half-Life 2"


@pytest.mark.asyncio
async def test_refresh_steam_library_replaces_stale_games_no_longer_owned(db):
    _connect_steam(db)
    db.add(SteamGame(appid=999, name="Old Game", playtime_forever_minutes=0, img_icon_url=""))
    db.commit()

    with patch("app.sync.steam_sync.steam_connector.fetch_owned_games", new=AsyncMock(return_value=[])):
        await steam_sync.refresh_steam_library(db)

    assert db.get(SteamGame, 999) is None


@pytest.mark.asyncio
async def test_refresh_steam_library_marks_credential_error_on_failure(db):
    _connect_steam(db)
    with patch("app.sync.steam_sync.steam_connector.fetch_owned_games", new=AsyncMock(side_effect=ValueError("private profile"))):
        with pytest.raises(ValueError):
            await steam_sync.refresh_steam_library(db)

    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one()
    assert cred.status == STATUS_ERROR
    assert "private profile" in cred.last_error


@pytest.mark.asyncio
async def test_refresh_steam_library_triggers_entitlement_matching(db):
    _connect_steam(db)
    ent = _seed_bundle_and_entitlement(db, steam_app_id="220")
    games = [SteamGameData(appid=220, name="Half-Life 2", playtime_forever_minutes=60, img_icon_url="")]
    with patch("app.sync.steam_sync.steam_connector.fetch_owned_games", new=AsyncMock(return_value=games)):
        await steam_sync.refresh_steam_library(db)

    db.refresh(ent)
    assert ent.steam_owned is True


def test_match_entitlements_marks_owned_true_when_appid_present(db):
    ent = _seed_bundle_and_entitlement(db, steam_app_id="220")
    db.add(SteamGame(appid=220, name="Half-Life 2", playtime_forever_minutes=0, img_icon_url=""))
    db.commit()

    updated = steam_sync.match_entitlements_to_steam(db)

    assert updated == 1
    db.refresh(ent)
    assert ent.steam_owned is True


def test_match_entitlements_marks_owned_false_when_appid_absent_from_library(db):
    ent = _seed_bundle_and_entitlement(db, steam_app_id="220")
    # No SteamGame rows at all — nothing owned.
    steam_sync.match_entitlements_to_steam(db)
    db.refresh(ent)
    assert ent.steam_owned is False


def test_match_entitlements_leaves_rows_without_steam_app_id_as_unknown(db):
    ent = _seed_bundle_and_entitlement(db, steam_app_id=None)
    steam_sync.match_entitlements_to_steam(db)
    db.refresh(ent)
    assert ent.steam_owned is None  # "not yet checked", never guessed at


def test_match_entitlements_updates_previously_true_to_false_on_removed_game(db):
    ent = _seed_bundle_and_entitlement(db, steam_app_id="220", steam_owned=True)
    # SteamGame table is now empty (e.g. after a refund) — re-matching must flip it.
    steam_sync.match_entitlements_to_steam(db)
    db.refresh(ent)
    assert ent.steam_owned is False


def test_get_steam_credential_returns_none_when_not_configured(db):
    assert steam_sync.get_steam_credential(db) is None


def test_get_steam_credential_returns_decrypted_payload(db):
    _connect_steam(db)
    payload = steam_sync.get_steam_credential(db)
    assert payload == {"api_key": "k", "steamid64": "765..."}


def _steam_key(db, name, appid=None, key_type="steam", gamekey="GK2"):
    import json

    from app.models.bundle import Bundle

    if db.get(Bundle, gamekey) is None:
        db.add(Bundle(gamekey=gamekey, name="B2", raw_json="{}"))
    ent = BundleEntitlement(
        gamekey=gamekey, machine_name=f"m-{name}", keyindex=0, key_name=name, steam_app_id=appid,
        raw_json=json.dumps({"key_type": key_type}),
    )
    db.add(ent)
    db.commit()
    return ent


def test_match_entitlements_falls_back_to_title_when_humbles_appid_is_stale(db):
    # Real case: Humble reported appid 25408 for Kentucky Route Zero while the
    # library holds it as 231200.
    db.add(SteamGame(appid=231200, name="Kentucky Route Zero", playtime_forever_minutes=0, img_icon_url=""))
    ent = _steam_key(db, "Kentucky Route Zero", appid="25408")
    db.commit()

    steam_sync.match_entitlements_to_steam(db)

    db.refresh(ent)
    assert ent.steam_owned is True


def test_match_entitlements_title_match_ignores_punctuation(db):
    db.add(SteamGame(appid=402710, name="Osiris: New Dawn", playtime_forever_minutes=0, img_icon_url=""))
    ent = _steam_key(db, "Osiris New Dawn", appid="78679")
    db.commit()

    steam_sync.match_entitlements_to_steam(db)

    db.refresh(ent)
    assert ent.steam_owned is True


def test_match_entitlements_marks_appidless_steam_key_owned_by_title(db):
    db.add(SteamGame(appid=40720, name="Samorost 2", playtime_forever_minutes=0, img_icon_url=""))
    ent = _steam_key(db, "Samorost 2", appid=None)
    db.commit()

    steam_sync.match_entitlements_to_steam(db)

    db.refresh(ent)
    assert ent.steam_owned is True


def test_match_entitlements_leaves_unmatched_appidless_steam_key_unknown(db):
    db.add(SteamGame(appid=1, name="Other", playtime_forever_minutes=0, img_icon_url=""))
    ent = _steam_key(db, "Combo Pack", appid=None)
    db.commit()

    steam_sync.match_entitlements_to_steam(db)

    db.refresh(ent)
    assert ent.steam_owned is None


def test_match_entitlements_resets_appidless_key_when_game_leaves_library(db):
    ent = _steam_key(db, "Samorost 2", appid=None)
    ent.steam_owned = True
    db.commit()

    steam_sync.match_entitlements_to_steam(db)

    db.refresh(ent)
    assert ent.steam_owned is None


def test_match_entitlements_never_title_matches_non_steam_keys(db):
    db.add(SteamGame(appid=1, name="Shared Title", playtime_forever_minutes=0, img_icon_url=""))
    ent = _steam_key(db, "Shared Title", appid=None, key_type="origin")
    db.commit()

    steam_sync.match_entitlements_to_steam(db)

    db.refresh(ent)
    assert ent.steam_owned is None


def test_match_entitlements_still_false_when_neither_id_nor_title_match(db):
    db.add(SteamGame(appid=1, name="Unrelated", playtime_forever_minutes=0, img_icon_url=""))
    ent = _steam_key(db, "Europa Universalis IV: Dharma", appid="827250")
    db.commit()

    steam_sync.match_entitlements_to_steam(db)

    db.refresh(ent)
    assert ent.steam_owned is False
