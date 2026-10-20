from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import plex_connector


def _resp(json_body, status=200):
    return httpx.Response(status, json=json_body, request=httpx.Request("GET", "https://x"))


@pytest.mark.asyncio
async def test_fetch_libraries_parses_key_title_type():
    resp = _resp({"MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "type": "movie"}, {"key": "3", "title": "Audiobooks", "type": "artist"}]}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        libraries = await plex_connector.fetch_libraries("http://plex.example.com:32400", "tok")
    assert [lib.key for lib in libraries] == ["1", "3"]
    assert libraries[1].type == "artist"


@pytest.mark.asyncio
async def test_fetch_libraries_sends_plex_token_header_and_strips_trailing_slash():
    resp = _resp({"MediaContainer": {}})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await plex_connector.fetch_libraries("http://plex.example.com:32400/", "secret-token")
    args, kwargs = mock_get.call_args
    assert args[0] == "http://plex.example.com:32400/library/sections"
    assert kwargs["headers"]["X-Plex-Token"] == "secret-token"


@pytest.mark.asyncio
async def test_fetch_libraries_raises_on_401():
    resp = _resp({}, status=401)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError, match="rejected"):
            await plex_connector.fetch_libraries("http://plex.example.com:32400", "bad-token")


@pytest.mark.asyncio
async def test_check_credentials_lists_every_library():
    resp = _resp({"MediaContainer": {"Directory": [{"key": "3", "title": "Audiobooks", "type": "artist"}]}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await plex_connector.check_credentials("http://plex.example.com:32400", "tok")
    assert result.ok is True
    assert "Audiobooks" in result.message
    assert "key 3" in result.message


@pytest.mark.asyncio
async def test_check_credentials_requires_a_base_url():
    result = await plex_connector.check_credentials("", "tok")
    assert result.ok is False


@pytest.mark.asyncio
async def test_fetch_audiobook_items_parses_title_and_parent_title_as_author():
    resp = _resp({"MediaContainer": {"Metadata": [{"ratingKey": "100", "title": "A Book", "parentTitle": "An Author"}]}})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        items = await plex_connector.fetch_audiobook_items("http://plex.example.com:32400", "tok", library_id="3")
    assert len(items) == 1
    assert items[0].rating_key == "100"
    assert items[0].title == "A Book"
    assert items[0].author == "An Author"


@pytest.mark.asyncio
async def test_fetch_audiobook_items_sends_album_type_against_the_given_library():
    resp = _resp({"MediaContainer": {"Metadata": []}})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await plex_connector.fetch_audiobook_items("http://plex.example.com:32400", "tok", library_id="3")
    args, kwargs = mock_get.call_args
    assert args[0] == "http://plex.example.com:32400/library/sections/3/all"
    assert kwargs["params"]["type"] == 9


@pytest.mark.asyncio
async def test_fetch_audiobook_items_requires_a_library_id():
    with pytest.raises(ValueError, match="library section ID is required"):
        await plex_connector.fetch_audiobook_items("http://plex.example.com:32400", "tok", library_id="")
