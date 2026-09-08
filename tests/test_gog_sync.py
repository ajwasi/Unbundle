import json
from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.gog_connector import GogAuthError, GogGameData
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_GOG, STATUS_ERROR, STATUS_OK, Credential
from app.models.gog_game import GogGame
from app.security import decrypt_json, encrypt_json
from app.sync import gog_sync


def _connect_gog(db, refresh_token="original-refresh-token"):
    db.add(Credential(source=SOURCE_GOG, encrypted_payload=encrypt_json({"refresh_token": refresh_token}), status=STATUS_OK))
    db.commit()


def _seed_bundle_and_entitlement(db, gamekey="GK1", **overrides):
    from app.models.bundle import Bundle

    db.add(Bundle(gamekey=gamekey, name="B", raw_json="{}"))
    ent = BundleEntitlement(gamekey=gamekey, machine_name="m", keyindex=0, key_name="Some Game - GOG")
    for k, v in overrides.items():
        setattr(ent, k, v)
    db.add(ent)
    db.commit()
    db.refresh(ent)
    return ent


@pytest.mark.asyncio
async def test_refresh_gog_library_raises_when_not_connected(db):
    with pytest.raises(gog_sync.NotConnectedError):
        await gog_sync.refresh_gog_library(db)


@pytest.mark.asyncio
async def test_refresh_gog_library_upserts_games(db):
    _connect_gog(db)
    games = [GogGameData(product_id=1207660413, title="Shadowrun Returns", image_url="https://x/img.jpg")]
    with patch("app.sync.gog_sync.gog_connector.refresh_access_token", new=AsyncMock(return_value={"access_token": "AT"})):
        with patch("app.sync.gog_sync.gog_connector.fetch_owned_games", new=AsyncMock(return_value=games)):
            count = await gog_sync.refresh_gog_library(db)

    assert count == 1
    saved = db.get(GogGame, 1207660413)
    assert saved.title == "Shadowrun Returns"


@pytest.mark.asyncio
async def test_refresh_gog_library_persists_rotated_refresh_token(db):
    _connect_gog(db, refresh_token="old-token")
    with patch("app.sync.gog_sync.gog_connector.refresh_access_token", new=AsyncMock(return_value={"access_token": "AT", "refresh_token": "new-token"})):
        with patch("app.sync.gog_sync.gog_connector.fetch_owned_games", new=AsyncMock(return_value=[])):
            await gog_sync.refresh_gog_library(db)

    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one()
    assert decrypt_json(cred.encrypted_payload)["refresh_token"] == "new-token"


@pytest.mark.asyncio
async def test_refresh_gog_library_keeps_existing_token_if_not_rotated(db):
    _connect_gog(db, refresh_token="stable-token")
    with patch("app.sync.gog_sync.gog_connector.refresh_access_token", new=AsyncMock(return_value={"access_token": "AT"})):
        with patch("app.sync.gog_sync.gog_connector.fetch_owned_games", new=AsyncMock(return_value=[])):
            await gog_sync.refresh_gog_library(db)

    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one()
    assert decrypt_json(cred.encrypted_payload)["refresh_token"] == "stable-token"


@pytest.mark.asyncio
async def test_refresh_gog_library_marks_credential_error_on_failure(db):
    _connect_gog(db)
    with patch("app.sync.gog_sync.gog_connector.refresh_access_token", new=AsyncMock(side_effect=GogAuthError("token expired"))):
        with pytest.raises(GogAuthError):
            await gog_sync.refresh_gog_library(db)

    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one()
    assert cred.status == STATUS_ERROR
    assert "token expired" in cred.last_error


@pytest.mark.asyncio
async def test_refresh_gog_library_triggers_entitlement_matching(db):
    _connect_gog(db)
    ent = _seed_bundle_and_entitlement(db, gog_id="1207660413")
    games = [GogGameData(product_id=1207660413, title="Shadowrun Returns", image_url="")]
    with patch("app.sync.gog_sync.gog_connector.refresh_access_token", new=AsyncMock(return_value={"access_token": "AT"})):
        with patch("app.sync.gog_sync.gog_connector.fetch_owned_games", new=AsyncMock(return_value=games)):
            await gog_sync.refresh_gog_library(db)

    db.refresh(ent)
    assert ent.gog_owned is True


def test_match_entitlements_marks_owned_true_when_product_id_present(db):
    ent = _seed_bundle_and_entitlement(db, gog_id="1207660413")
    db.add(GogGame(product_id=1207660413, title="Shadowrun Returns", image_url=""))
    db.commit()

    updated = gog_sync.match_entitlements_to_gog(db)

    assert updated == 1
    db.refresh(ent)
    assert ent.gog_owned is True


def test_match_entitlements_marks_owned_false_when_absent_from_library(db):
    ent = _seed_bundle_and_entitlement(db, gog_id="1207660413")
    gog_sync.match_entitlements_to_gog(db)
    db.refresh(ent)
    assert ent.gog_owned is False


def test_match_entitlements_falls_back_to_name_match_when_no_gog_id(db):
    # The realistic case: confirmed against the real dev library that GOG-type
    # entitlements essentially never carry a gog_id, but Humble's key_name for
    # them has matched the real GOG catalog title exactly in every case seen.
    ent = _seed_bundle_and_entitlement(
        db, gog_id=None, key_name="Liberated", raw_json=json.dumps({"key_type": "gog"})
    )
    db.add(GogGame(product_id=1780442795, title="Liberated", image_url=""))
    db.commit()

    updated = gog_sync.match_entitlements_to_gog(db)

    assert updated == 1
    db.refresh(ent)
    assert ent.gog_owned is True


def test_match_entitlements_name_match_is_case_insensitive(db):
    ent = _seed_bundle_and_entitlement(
        db, gog_id=None, key_name="liberated", raw_json=json.dumps({"key_type": "gog"})
    )
    db.add(GogGame(product_id=1780442795, title="Liberated", image_url=""))
    db.commit()

    gog_sync.match_entitlements_to_gog(db)

    db.refresh(ent)
    assert ent.gog_owned is True


def test_match_entitlements_name_match_false_when_title_not_in_library(db):
    ent = _seed_bundle_and_entitlement(
        db, gog_id=None, key_name="Some Unowned Game", raw_json=json.dumps({"key_type": "gog"})
    )
    gog_sync.match_entitlements_to_gog(db)
    db.refresh(ent)
    assert ent.gog_owned is False


def test_match_entitlements_does_not_name_match_non_gog_key_types(db):
    # key_name is generic (every entitlement has one) — without the key_type
    # check, an origin/uplay/generic key sharing a title with a GOG game would
    # get a false-positive match.
    ent = _seed_bundle_and_entitlement(
        db, gog_id=None, key_name="Liberated", raw_json=json.dumps({"key_type": "origin"})
    )
    db.add(GogGame(product_id=1780442795, title="Liberated", image_url=""))
    db.commit()

    updated = gog_sync.match_entitlements_to_gog(db)

    assert updated == 0
    db.refresh(ent)
    assert ent.gog_owned is None


def test_get_gog_credential_returns_none_when_not_configured(db):
    assert gog_sync.get_gog_credential(db) is None


def test_save_refresh_token_creates_credential_if_missing(db):
    gog_sync.save_refresh_token(db, "new-token")
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one()
    assert decrypt_json(cred.encrypted_payload)["refresh_token"] == "new-token"
