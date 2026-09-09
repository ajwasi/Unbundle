"""Exercises the real connectors and real sync pipelines against the real
mock API server (see tests/mock_server.py, mock_api/) — a genuine end-to-end
pass using the curated demo dataset (scripts/generate_mock_data.py), rather
than the narrow single-purpose fixtures the rest of the suite hand-builds.
Complements, not replaces, the existing unittest.mock-based connector tests
(those cover credential-check edge cases this mock server deliberately never
exercises, since every mock route always succeeds).
"""

import pytest

from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_GOG, SOURCE_HUMBLE, SOURCE_STEAM, Credential
from app.security import encrypt_json
from app.sync import gog_sync, refresh, steam_sync

_NOOP_LOG = lambda level, message: None  # noqa: E731


def _seed_humble_credential(db):
    db.add(Credential(source=SOURCE_HUMBLE, encrypted_payload=encrypt_json({"session_key": "demo-session-key"})))
    db.commit()


def _seed_steam_credential(db):
    db.add(
        Credential(
            source=SOURCE_STEAM,
            encrypted_payload=encrypt_json({"api_key": "demo-api-key", "steamid64": "76500000000000001"}),
        )
    )
    db.commit()


def _seed_gog_credential(db):
    db.add(Credential(source=SOURCE_GOG, encrypted_payload=encrypt_json({"refresh_token": "demo-refresh-token"})))
    db.commit()


@pytest.mark.asyncio
async def test_humble_sync_against_mock_server_populates_the_curated_bundles(demo_mode, db):
    _seed_humble_credential(db)

    count = await refresh.refresh_library(db, _NOOP_LOG)

    assert count == 14
    assert db.query(Bundle).count() == 14
    categories = {b.category for b in db.query(Bundle).all()}
    assert categories == {"bundle", "storefront", "subscriptioncontent", "subscriptionplan", "widget"}

    sonic = db.get(Bundle, "8avFvGDh6eAF7uRp")
    assert sonic.name == "Humble Sonic Bundle 2019"
    assert sonic.subproduct_count == 0  # key-only bundle
    assert sonic.key_count == 13

    entitlements = db.query(BundleEntitlement).filter(BundleEntitlement.gamekey == "8avFvGDh6eAF7uRp").all()
    # A mix, matching the real account (10 of 13 redeemed) — the "Unredeemed"
    # badge case needs at least one holdout to survive, not all of them.
    assert any(not e.redeemed_on_humble for e in entitlements)
    assert any(e.redeemed_on_humble for e in entitlements)


@pytest.mark.asyncio
async def test_steam_cross_check_against_mock_server_distinguishes_owned_from_unredeemed(demo_mode, db):
    _seed_humble_credential(db)
    await refresh.refresh_library(db, _NOOP_LOG)
    _seed_steam_credential(db)

    game_count = await steam_sync.refresh_steam_library(db)
    assert game_count == 3

    owned = {
        e.steam_app_id
        for e in db.query(BundleEntitlement).filter(BundleEntitlement.steam_owned.is_(True)).all()
    }
    assert owned == {"202530", "203650", "61510"}

    # Same bundles have other Steam-type keys deliberately left unowned in the
    # curated dataset (see scripts/generate_mock_data.py) — the "genuinely
    # never redeemed, not Steam-matched" case must survive, not get erased.
    unowned = db.query(BundleEntitlement).filter(
        BundleEntitlement.gamekey == "8avFvGDh6eAF7uRp", BundleEntitlement.steam_owned.is_(False)
    ).count()
    assert unowned > 0


@pytest.mark.asyncio
async def test_gog_name_matching_against_mock_server_matches_the_curated_titles(demo_mode, db):
    _seed_humble_credential(db)
    await refresh.refresh_library(db, _NOOP_LOG)
    _seed_gog_credential(db)

    game_count = await gog_sync.refresh_gog_library(db)
    assert game_count == 2

    matched_titles = {
        e.key_name
        for e in db.query(BundleEntitlement).filter(BundleEntitlement.gog_owned.is_(True)).all()
    }
    assert matched_titles == {"Liberated", "Wanderlust: Travel Stories"}


@pytest.mark.asyncio
async def test_curated_library_renders_through_the_real_pages_without_errors(demo_mode, db, authed_client):
    _seed_humble_credential(db)
    await refresh.refresh_library(db, _NOOP_LOG)

    for path in ("/bundles", "/catalog", "/finance", "/"):
        resp = authed_client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"

    # The multi-format MIT Press bundle and the key-only Sonic bundle are the
    # two structural edge cases most likely to break a naive template — check
    # both actually made it onto the Bundles page, not just that *a* 200 came back.
    bundles_page = authed_client.get("/bundles").text
    assert "Essential Knowledge" in bundles_page
    assert "Humble Sonic Bundle 2019" in bundles_page
