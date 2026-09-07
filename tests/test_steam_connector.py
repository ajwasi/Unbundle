from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import steam_connector


def _resp(json_body, status=200):
    return httpx.Response(status, json=json_body, request=httpx.Request("GET", "https://x"))


@pytest.mark.asyncio
async def test_resolve_steamid_passes_through_a_raw_17_digit_id():
    result = await steam_connector.resolve_steamid("key", "76561198010668763")
    assert result == "76561198010668763"


@pytest.mark.asyncio
async def test_resolve_steamid_calls_resolve_vanity_url_for_a_name():
    resp = _resp({"response": {"success": 1, "steamid": "76561198010668763"}})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        result = await steam_connector.resolve_steamid("key", "myusername")
    assert result == "76561198010668763"
    assert mock_get.call_args.kwargs["params"]["vanityurl"] == "myusername"


@pytest.mark.asyncio
async def test_resolve_steamid_extracts_vanity_name_from_profile_url():
    resp = _resp({"response": {"success": 1, "steamid": "76561198010668763"}})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await steam_connector.resolve_steamid("key", "https://steamcommunity.com/id/myusername/")
    assert mock_get.call_args.kwargs["params"]["vanityurl"] == "myusername"


@pytest.mark.asyncio
async def test_resolve_steamid_raises_when_not_found():
    resp = _resp({"response": {"success": 42, "message": "No match"}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError):
            await steam_connector.resolve_steamid("key", "nonexistent-user")


@pytest.mark.asyncio
async def test_check_credentials_ok_for_public_profile():
    resp = _resp({"response": {"players": [{"personaname": "TestUser", "communityvisibilitystate": 3}]}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await steam_connector.check_credentials("key", "765...")
    assert result.ok
    assert "TestUser" in result.message


@pytest.mark.asyncio
async def test_check_credentials_rejects_private_profile():
    resp = _resp({"response": {"players": [{"personaname": "TestUser", "communityvisibilitystate": 1}]}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await steam_connector.check_credentials("key", "765...")
    assert not result.ok
    assert "private" in result.message.lower()


@pytest.mark.asyncio
async def test_check_credentials_rejects_unknown_steamid():
    resp = _resp({"response": {"players": []}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await steam_connector.check_credentials("key", "765...")
    assert not result.ok


@pytest.mark.asyncio
async def test_check_credentials_rejects_403():
    resp = _resp({}, status=403)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await steam_connector.check_credentials("bad-key", "765...")
    assert not result.ok
    assert "rejected" in result.message.lower()


@pytest.mark.asyncio
async def test_fetch_owned_games_parses_real_shape():
    resp = _resp(
        {
            "response": {
                "game_count": 2,
                "games": [
                    {"appid": 220, "name": "Half-Life 2", "playtime_forever": 120, "img_icon_url": "abc"},
                    {"appid": 400, "name": "Portal", "playtime_forever": 0, "img_icon_url": ""},
                ],
            }
        }
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        games = await steam_connector.fetch_owned_games("key", "765...")
    assert len(games) == 2
    hl2 = next(g for g in games if g.appid == 220)
    assert hl2.name == "Half-Life 2"
    assert hl2.playtime_forever_minutes == 120


@pytest.mark.asyncio
async def test_fetch_owned_games_raises_on_empty_response_private_profile():
    # Confirmed real Steam API behavior: a private/hidden-game-details profile
    # comes back 200 with an empty {"response": {}}, no error status at all.
    resp = _resp({"response": {}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError):
            await steam_connector.fetch_owned_games("key", "765...")


@pytest.mark.asyncio
async def test_fetch_owned_games_handles_missing_name_gracefully():
    resp = _resp({"response": {"games": [{"appid": 999, "playtime_forever": 0}]}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        games = await steam_connector.fetch_owned_games("key", "765...")
    assert games[0].name == "App 999"
