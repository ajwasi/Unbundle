from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import navidrome_connector


def _resp(subsonic_body, status=200):
    return httpx.Response(status, json={"subsonic-response": subsonic_body}, request=httpx.Request("GET", "https://x"))


@pytest.mark.asyncio
async def test_auth_params_use_a_fresh_salted_token_each_call():
    first = navidrome_connector._auth_params("alice", "secret")
    second = navidrome_connector._auth_params("alice", "secret")
    assert first["u"] == "alice"
    assert first["s"] != second["s"]
    assert first["t"] != second["t"]
    assert first["f"] == "json"


@pytest.mark.asyncio
async def test_check_credentials_ok_on_ping_success():
    resp = _resp({"status": "ok"})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await navidrome_connector.check_credentials("https://nd.example.com", "alice", "secret")
    assert result.ok is True
    assert "alice" in result.message


@pytest.mark.asyncio
async def test_check_credentials_fails_on_subsonic_error_status():
    resp = _resp({"status": "failed", "error": {"code": 40, "message": "Wrong username or password."}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await navidrome_connector.check_credentials("https://nd.example.com", "alice", "wrong")
    assert result.ok is False
    assert "Wrong username or password" in result.message


@pytest.mark.asyncio
async def test_check_credentials_requires_a_base_url():
    result = await navidrome_connector.check_credentials("", "alice", "secret")
    assert result.ok is False


@pytest.mark.asyncio
async def test_fetch_albums_parses_id_name_artist():
    resp = _resp({"status": "ok", "albumList2": {"album": [{"id": "al1", "name": "An Album", "artist": "An Artist"}]}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        albums = await navidrome_connector.fetch_albums("https://nd.example.com", "alice", "secret")
    assert len(albums) == 1
    assert albums[0].album_id == "al1"
    assert albums[0].name == "An Album"
    assert albums[0].artist == "An Artist"


@pytest.mark.asyncio
async def test_fetch_albums_pages_until_a_short_page():
    full_page = [{"id": f"al{i}", "name": f"Album {i}", "artist": "A"} for i in range(navidrome_connector._PAGE_SIZE)]
    short_page = [{"id": "last", "name": "Last Album", "artist": "A"}]
    responses = [
        _resp({"status": "ok", "albumList2": {"album": full_page}}),
        _resp({"status": "ok", "albumList2": {"album": short_page}}),
    ]
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", new=mock_get):
        albums = await navidrome_connector.fetch_albums("https://nd.example.com", "alice", "secret")
    assert len(albums) == navidrome_connector._PAGE_SIZE + 1
    assert mock_get.call_count == 2
    second_call_params = mock_get.call_args_list[1].kwargs["params"]
    assert second_call_params["offset"] == navidrome_connector._PAGE_SIZE
