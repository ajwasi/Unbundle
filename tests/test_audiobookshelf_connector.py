from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import audiobookshelf_connector


def _resp(json_body, status=200):
    return httpx.Response(status, json=json_body, request=httpx.Request("GET", "https://x"))


@pytest.mark.asyncio
async def test_fetch_libraries_parses_id_name_media_type():
    resp = _resp({"libraries": [{"id": "lib1", "name": "Audiobooks", "mediaType": "book"}, {"id": "lib2", "name": "Podcasts", "mediaType": "podcast"}]})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        libraries = await audiobookshelf_connector.fetch_libraries("https://abs.example.com", "tok")
    assert [lib.id for lib in libraries] == ["lib1", "lib2"]
    assert libraries[0].media_type == "book"


@pytest.mark.asyncio
async def test_fetch_libraries_sends_bearer_token_and_strips_trailing_slash():
    resp = _resp({"libraries": []})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await audiobookshelf_connector.fetch_libraries("https://abs.example.com/", "secret-token")
    args, kwargs = mock_get.call_args
    assert args[0] == "https://abs.example.com/api/libraries"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_fetch_libraries_raises_on_401():
    resp = _resp({}, status=401)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError, match="rejected"):
            await audiobookshelf_connector.fetch_libraries("https://abs.example.com", "bad-token")


@pytest.mark.asyncio
async def test_check_credentials_ok_when_a_book_library_exists():
    resp = _resp({"libraries": [{"id": "lib1", "name": "Audiobooks", "mediaType": "book"}]})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await audiobookshelf_connector.check_credentials("https://abs.example.com", "tok")
    assert result.ok is True
    assert "Audiobooks" in result.message


@pytest.mark.asyncio
async def test_check_credentials_fails_when_no_book_library_exists():
    resp = _resp({"libraries": [{"id": "lib1", "name": "Podcasts", "mediaType": "podcast"}]})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        result = await audiobookshelf_connector.check_credentials("https://abs.example.com", "tok")
    assert result.ok is False
    assert "no audiobook library" in result.message.lower()


@pytest.mark.asyncio
async def test_check_credentials_requires_a_base_url():
    result = await audiobookshelf_connector.check_credentials("", "tok")
    assert result.ok is False


@pytest.mark.asyncio
async def test_fetch_library_items_parses_asin_title_author_from_metadata():
    resp = _resp(
        {
            "results": [
                {"id": "item1", "media": {"metadata": {"asin": "B001", "title": "A Book", "authorName": "An Author"}}},
                {"id": "item2", "media": {"metadata": {"title": "No ASIN Book", "authorName": "Someone"}}},
            ]
        }
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        items = await audiobookshelf_connector.fetch_library_items("https://abs.example.com", "tok", library_id="lib1")
    assert len(items) == 2
    assert items[0].asin == "B001"
    assert items[0].title == "A Book"
    assert items[0].author == "An Author"
    assert items[1].asin == ""


@pytest.mark.asyncio
async def test_fetch_library_items_auto_picks_the_first_book_library_when_none_given():
    libraries_resp = _resp({"libraries": [{"id": "lib1", "name": "Podcasts", "mediaType": "podcast"}, {"id": "lib2", "name": "Audiobooks", "mediaType": "book"}]})
    items_resp = _resp({"results": []})
    mock_get = AsyncMock(side_effect=[libraries_resp, items_resp])
    with patch("httpx.AsyncClient.get", new=mock_get):
        await audiobookshelf_connector.fetch_library_items("https://abs.example.com", "tok")
    second_call_url = mock_get.call_args_list[1].args[0]
    assert "lib2" in second_call_url


@pytest.mark.asyncio
async def test_fetch_library_items_raises_when_no_book_library_found_and_none_specified():
    resp = _resp({"libraries": [{"id": "lib1", "name": "Podcasts", "mediaType": "podcast"}]})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError, match="No audiobook library"):
            await audiobookshelf_connector.fetch_library_items("https://abs.example.com", "tok")
