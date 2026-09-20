from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import gog_wishlist as gw
from app.connectors import steam_wishlist as sw
from app.models.credential import SOURCE_GOG, SOURCE_STEAM, STATUS_OK, Credential
from app.models.gog_game import GogGame
from app.models.steam_game import SteamGame
from app.models.store_wishlist import (
    GogWishlistItem,
    GogWishlistPrice,
    SteamWishlistItem,
    SteamWishlistPrice,
)
from app.security import encrypt_json
from app.sync import store_wishlist_sync as sync


# ======================================================================
# Steam
# ======================================================================


def test_steam_wishlist_parses_appids_priority_and_date():
    payload = {
        "response": {
            "items": [
                {"appid": 220, "priority": 1, "date_added": 1700000000},
                {"appid": 620, "priority": 2},
            ]
        }
    }
    entries = sw.parse_wishlist(payload)

    assert [e.appid for e in entries] == [220, 620]
    assert entries[0].priority == 1
    assert entries[0].added_at == datetime.utcfromtimestamp(1700000000)
    assert entries[1].added_at is None


def test_an_empty_steam_response_is_not_an_error():
    # A private profile and an empty wishlist look identical from here, and
    # neither is a failure worth surfacing as one.
    assert sw.parse_wishlist({"response": {}}) == []
    assert sw.parse_wishlist({}) == []


def test_steam_prices_are_converted_from_minor_units():
    payload = {
        "220": {
            "success": True,
            "data": {
                "name": "Half-Life 2",
                "header_image": "https://cdn/220.jpg",
                "developers": ["Valve"],
                "short_description": "A game.",
                "price_overview": {"currency": "USD", "initial": 999, "final": 499, "discount_percent": 50},
            },
        }
    }
    details = sw.parse_app_details(220, payload)

    assert details.name == "Half-Life 2"
    assert details.developers == "Valve"
    assert details.current_price == 4.99
    assert details.list_price == 9.99
    assert details.discount_pct == 50
    assert details.currency == "USD"


def test_a_free_or_unpriced_steam_app_yields_no_price():
    payload = {"220": {"success": True, "data": {"name": "Free Game"}}}
    details = sw.parse_app_details(220, payload)

    assert details.name == "Free Game"
    assert details.current_price is None
    assert details.discount_pct == 0


def test_an_unsuccessful_steam_app_is_none():
    # Delisted or region-locked: ordinary, not an error.
    assert sw.parse_app_details(220, {"220": {"success": False}}) is None


# ======================================================================
# GOG
# ======================================================================


def test_gog_wishlist_keeps_only_flagged_ids():
    payload = {"wishlist": {"1207658924": True, "1211776926": True, "999": False}}
    assert sorted(gw.parse_wishlist(payload)) == [1207658924, 1211776926]


def test_gog_wishlist_tolerates_a_missing_key():
    assert gw.parse_wishlist({}) == []
    assert gw.parse_wishlist({"wishlist": []}) == []


def test_gog_prices_are_parsed_from_the_batch_shape():
    payload = {
        "_embedded": {
            "items": [
                {
                    "_embedded": {
                        "product": {"id": 1211776926},
                        "prices": [{"currency": {"code": "USD"}, "basePrice": "2959 USD", "finalPrice": "149 USD"}],
                    }
                },
                {"_embedded": {"product": {"id": 1207658924}, "prices": []}},
            ]
        }
    }
    prices = {p.product_id: p for p in gw.parse_prices(payload)}

    assert prices[1211776926].list_price == 29.59
    assert prices[1211776926].current_price == 1.49
    assert prices[1211776926].currency == "USD"
    # No price block is a legitimate answer, not a crash.
    assert prices[1207658924].current_price is None


def test_gog_product_details_normalise_a_protocol_relative_cover():
    title, cover, url = gw.parse_product(
        {
            "title": "Dead Age",
            "images": {"logo2x": "//images.gog.com/abc.jpg"},
            "links": {"product_card": "https://www.gog.com/game/dead_age"},
        }
    )
    assert title == "Dead Age"
    assert cover == "https://images.gog.com/abc.jpg"
    assert url == "https://www.gog.com/game/dead_age"


@pytest.mark.parametrize(
    "item,expected",
    [(GogWishlistItem(current_price=1.49, list_price=29.59), 95), (GogWishlistItem(current_price=5.0, list_price=5.0), None)],
)
def test_gog_discount_is_derived_only_when_meaningful(item, expected):
    assert item.discount_pct == expected


# ======================================================================
# Sync
# ======================================================================


def _connect_steam(db):
    db.add(
        Credential(
            source=SOURCE_STEAM,
            status=STATUS_OK,
            encrypted_payload=encrypt_json({"api_key": "k", "steamid64": "765611980"}),
        )
    )
    db.commit()


def _connect_gog(db):
    db.add(
        Credential(source=SOURCE_GOG, status=STATUS_OK, encrypted_payload=encrypt_json({"refresh_token": "rt"}))
    )
    db.commit()


def _details(price=4.99, list_price=9.99, discount=50, name="Half-Life 2"):
    return sw.SteamAppDetails(
        name=name,
        header_image="https://cdn/220.jpg",
        developers="Valve",
        short_description="A game.",
        current_price=price,
        list_price=list_price,
        currency="USD",
        discount_pct=discount,
    )


@pytest.mark.asyncio
async def test_steam_sync_stores_items_and_prices(db):
    _connect_steam(db)
    entries = [sw.SteamWishlistEntry(appid=220, priority=1, added_at=None)]

    with patch.object(sw, "fetch_wishlist", new=AsyncMock(return_value=entries)), patch.object(
        sw, "fetch_app_details", new=AsyncMock(return_value=_details())
    ), patch("asyncio.sleep", new=AsyncMock()):
        result = await sync.refresh_steam_wishlist(db)

    assert result["total"] == 1
    assert result["new"] == 1
    assert result["detail_fetches"] == 1
    row = db.get(SteamWishlistItem, 220)
    assert row.name == "Half-Life 2"
    assert row.current_price == 4.99
    assert db.query(SteamWishlistPrice).count() == 1


@pytest.mark.asyncio
async def test_steam_sync_only_fetches_full_details_once_per_app(db):
    _connect_steam(db)
    entries = [sw.SteamWishlistEntry(appid=220, priority=1, added_at=None)]

    with patch.object(sw, "fetch_wishlist", new=AsyncMock(return_value=entries)), patch.object(
        sw, "fetch_app_details", new=AsyncMock(return_value=_details())
    ) as details_mock, patch("asyncio.sleep", new=AsyncMock()):
        await sync.refresh_steam_wishlist(db)
        second = await sync.refresh_steam_wishlist(db)

        # Second pass asks for price only — the expensive fields never change.
        assert details_mock.await_args.kwargs["price_only"] is True

    assert second["detail_fetches"] == 0
    assert db.query(SteamWishlistPrice).count() == 1  # price unchanged


@pytest.mark.asyncio
async def test_steam_sync_requires_a_credential(db):
    with pytest.raises(sync.NotConnectedError):
        await sync.refresh_steam_wishlist(db)


@pytest.mark.asyncio
async def test_gog_sync_prices_the_whole_wishlist_in_one_batch(db):
    _connect_gog(db)
    prices = [gw.GogWishlistPriceData(1, 1.49, 29.59, "USD"), gw.GogWishlistPriceData(2, 5.0, 5.0, "USD")]

    with patch("app.connectors.gog_connector.refresh_access_token", new=AsyncMock(return_value={"access_token": "at", "refresh_token": "rt"})), \
         patch.object(gw, "fetch_wishlist_ids", new=AsyncMock(return_value=[1, 2])), \
         patch.object(gw, "fetch_prices", new=AsyncMock(return_value=prices)) as price_mock, \
         patch.object(gw, "fetch_product", new=AsyncMock(return_value=("A Game", "https://c/x.jpg", "https://gog/x"))):
        result = await sync.refresh_gog_wishlist(db)

    assert price_mock.await_count == 1  # one batch, not one per product
    assert result["total"] == 2
    assert db.get(GogWishlistItem, 1).current_price == 1.49
    assert db.query(GogWishlistPrice).count() == 2


@pytest.mark.asyncio
async def test_gog_sync_requires_a_credential(db):
    with pytest.raises(sync.NotConnectedError):
        await sync.refresh_gog_wishlist(db)


# ======================================================================
# Shared rules
# ======================================================================


@pytest.mark.asyncio
async def test_a_source_returning_nothing_does_not_flag_the_whole_table(db):
    # Far more likely a transient failure than a genuinely emptied wishlist,
    # and acting on it would wipe every row's status at once.
    _connect_steam(db)
    now = datetime.utcnow()
    db.add(SteamWishlistItem(appid=220, name="Kept", first_seen_at=now, last_seen_at=now))
    db.commit()

    with patch.object(sw, "fetch_wishlist", new=AsyncMock(return_value=[])), patch("asyncio.sleep", new=AsyncMock()):
        result = await sync.refresh_steam_wishlist(db)

    assert result["removed"] == 0
    assert db.get(SteamWishlistItem, 220).removed_at is None


# ======================================================================
# Page
# ======================================================================


def _seed_steam(db, appid=220, name="Half-Life 2", price=4.99, low=4.99):
    now = datetime.utcnow()
    db.add(
        SteamWishlistItem(
            appid=appid, name=name, developers="Valve", current_price=price, list_price=9.99,
            currency="USD", discount_pct=50, first_seen_at=now, last_seen_at=now,
        )
    )
    db.add(SteamWishlistPrice(appid=appid, price=low, currency="USD", captured_at=now))
    db.commit()


def _seed_gog(db, product_id=1, title="Dead Age", price=1.49):
    now = datetime.utcnow()
    db.add(
        GogWishlistItem(
            product_id=product_id, title=title, current_price=price, list_price=29.59,
            currency="USD", first_seen_at=now, last_seen_at=now,
        )
    )
    db.add(GogWishlistPrice(product_id=product_id, price=price, currency="USD", captured_at=now))
    db.commit()


def test_page_merges_all_three_sources(authed_client, db):
    _seed_steam(db)
    _seed_gog(db)
    resp = authed_client.get("/wishlist")

    assert "Half-Life 2" in resp.text
    assert "Dead Age" in resp.text
    assert ">Steam<" in resp.text
    assert ">GOG<" in resp.text


def test_source_filter_narrows_to_one_store(authed_client, db):
    _seed_steam(db)
    _seed_gog(db)

    resp = authed_client.get("/wishlist", params={"source": "gog"})
    assert "Dead Age" in resp.text
    assert "Half-Life 2" not in resp.text


def test_owned_is_matched_per_store(authed_client, db):
    _seed_steam(db)
    _seed_gog(db)
    db.add(SteamGame(appid=220, name="Half-Life 2", playtime_forever_minutes=0))
    db.add(GogGame(product_id=999, title="Something Else"))
    db.commit()

    resp = authed_client.get("/wishlist")
    # Steam row owned, GOG row not — the GOG library holds a different id.
    assert resp.text.count(">Owned<") == 1


def test_lowest_yet_badge_reflects_price_history(authed_client, db):
    _seed_steam(db, appid=220, price=4.99, low=4.99)
    _seed_gog(db)
    db.add(SteamWishlistPrice(appid=220, price=2.49, currency="USD", captured_at=datetime.utcnow()))
    db.commit()

    resp = authed_client.get("/wishlist")
    # Current 4.99 is above the 2.49 low, so it must not claim lowest-yet.
    assert resp.text.count("Lowest yet") == 1  # only the GOG row qualifies


def test_an_unknown_source_is_a_404(authed_client):
    assert authed_client.post("/wishlist/refresh/nintendo").status_code == 404


def test_refresh_without_steam_connected_explains_rather_than_500s(authed_client):
    resp = authed_client.post("/wishlist/refresh/steam")
    assert resp.status_code == 200
    assert "not connected yet" in resp.text


def test_refresh_without_gog_connected_explains_rather_than_500s(authed_client):
    resp = authed_client.post("/wishlist/refresh/gog")
    assert resp.status_code == 200
    assert "not connected yet" in resp.text


def test_a_broken_source_reads_as_one_connector_failing(authed_client, db):
    _connect_steam(db)
    with patch.object(sync, "refresh_steam_wishlist", new=AsyncMock(side_effect=KeyError("items"))):
        resp = authed_client.post("/wishlist/refresh/steam")

    assert resp.status_code == 200
    assert "Steam wishlist refresh failed" in resp.text
