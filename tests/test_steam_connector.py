from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.connectors import steam_connector


def test_base_url_is_real_by_default():
    assert steam_connector._base_url() == steam_connector.BASE_URL


def test_base_url_redirects_to_the_mock_server_in_demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "mock_api_base_url", "http://127.0.0.1:9999")
    assert steam_connector._base_url() == "http://127.0.0.1:9999/steam"


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
async def test_resolve_steamid_error_never_leaks_the_api_key():
    # Real bug, confirmed and fixed: resp.raise_for_status() embeds the full
    # request URL (including ?key=...) in its exception message, which flowed
    # into Credential.last_error (plaintext) and the Settings UI. Any non-200
    # here must produce a message with no trace of the key.
    real_key = "TOTALLY-SECRET-STEAM-KEY-XYZ"
    resp = httpx.Response(
        403,
        request=httpx.Request("GET", "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/", params={"key": real_key, "vanityurl": "someone"}),
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError) as exc_info:
            await steam_connector.resolve_steamid(real_key, "someone")
    assert real_key not in str(exc_info.value)


@pytest.mark.asyncio
async def test_resolve_steamid_raises_clean_message_on_non_200():
    resp = httpx.Response(500, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError, match="HTTP 500"):
            await steam_connector.resolve_steamid("key", "someone")


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


@pytest.mark.asyncio
async def test_fetch_owned_games_error_never_leaks_the_api_key():
    real_key = "TOTALLY-SECRET-STEAM-KEY-XYZ"
    resp = httpx.Response(
        403,
        request=httpx.Request("GET", "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/", params={"key": real_key, "steamid": "765..."}),
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError) as exc_info:
            await steam_connector.fetch_owned_games(real_key, "765...")
    assert real_key not in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_owned_games_raises_clean_message_on_non_200():
    resp = httpx.Response(500, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError, match="HTTP 500"):
            await steam_connector.fetch_owned_games("key", "765...")
