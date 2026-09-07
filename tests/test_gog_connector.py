from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.connectors import gog_connector


def _resp(json_body, status=200):
    return httpx.Response(status, json=json_body, request=httpx.Request("GET", "https://x"))


def test_login_url_is_fixed_and_uses_gogs_own_registered_redirect():
    # Confirmed the hard way: a self-hosted redirect_uri gets redirect_uri_mismatch
    # once a real login actually completes, even though GOG's /auth endpoint will
    # happily *serve a page* for any redirect_uri (that's not the same thing as
    # accepting it at token-issue time). REDIRECT_URI must stay GOG's own.
    parsed = urlparse(gog_connector.LOGIN_URL)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "auth.gog.com"
    assert params["client_id"] == [gog_connector.CLIENT_ID]
    assert params["redirect_uri"] == [gog_connector.REDIRECT_URI]
    assert params["response_type"] == ["code"]


def test_extract_code_from_full_redirected_url():
    url = "https://embed.gog.com/on_login_success?origin=client&code=abc123XYZ"
    assert gog_connector.extract_code(url) == "abc123XYZ"


def test_extract_code_from_bare_code():
    assert gog_connector.extract_code("abc123XYZ") == "abc123XYZ"


def test_extract_code_strips_whitespace():
    assert gog_connector.extract_code("  abc123XYZ  ") == "abc123XYZ"


def test_extract_code_handles_code_not_last_param():
    url = "https://embed.gog.com/on_login_success?code=abc123&origin=client"
    assert gog_connector.extract_code(url) == "abc123"


@pytest.mark.asyncio
async def test_exchange_code_success():
    resp = _resp({"access_token": "AT", "refresh_token": "RT"})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        tokens = await gog_connector.exchange_code("some-code")
    assert tokens == {"access_token": "AT", "refresh_token": "RT"}
    params = mock_get.call_args.kwargs["params"]
    assert params["grant_type"] == "authorization_code"
    assert params["code"] == "some-code"
    assert params["client_secret"] == gog_connector.CLIENT_SECRET
    assert params["redirect_uri"] == gog_connector.REDIRECT_URI


@pytest.mark.asyncio
async def test_exchange_code_raises_on_non_200():
    resp = _resp({"error": "invalid_grant"}, status=400)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog_connector.GogAuthError):
            await gog_connector.exchange_code("expired-code")


@pytest.mark.asyncio
async def test_exchange_code_error_surfaces_gogs_actual_reason():
    # The generic "may have expired" guess isn't useful for telling a real
    # invalid_grant/expired code apart from e.g. a redirect_uri_mismatch bug on
    # our own end — the real error/error_description must be visible.
    resp = _resp({"error": "invalid_grant", "error_description": "The authorization code has expired"}, status=400)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog_connector.GogAuthError, match="invalid_grant"):
            await gog_connector.exchange_code("expired-code")


@pytest.mark.asyncio
async def test_exchange_code_error_falls_back_to_status_when_body_has_no_error_fields():
    resp = _resp({"something": "else"}, status=500)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog_connector.GogAuthError, match="HTTP 500"):
            await gog_connector.exchange_code("code")


@pytest.mark.asyncio
async def test_exchange_code_error_falls_back_when_body_is_not_json():
    resp = httpx.Response(400, text="<html>not json</html>", request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog_connector.GogAuthError, match="HTTP 400"):
            await gog_connector.exchange_code("code")


@pytest.mark.asyncio
async def test_exchange_code_raises_when_tokens_missing_from_response():
    resp = _resp({"unexpected": "shape"})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog_connector.GogAuthError):
            await gog_connector.exchange_code("code")


@pytest.mark.asyncio
async def test_refresh_access_token_uses_refresh_token_grant():
    resp = _resp({"access_token": "AT2", "refresh_token": "RT2"})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        tokens = await gog_connector.refresh_access_token("old-refresh-token")
    assert tokens["access_token"] == "AT2"
    params = mock_get.call_args.kwargs["params"]
    assert params["grant_type"] == "refresh_token"
    assert params["refresh_token"] == "old-refresh-token"


@pytest.mark.asyncio
async def test_refresh_access_token_raises_on_rejected_token():
    resp = _resp({}, status=401)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog_connector.GogAuthError):
            await gog_connector.refresh_access_token("stale-token")


@pytest.mark.asyncio
async def test_fetch_owned_games_parses_real_shape_and_filters_to_games():
    page_response = {
        "page": 1,
        "totalPages": 1,
        "products": [
            {"id": 1207660413, "title": "Shadowrun Returns", "image": "//images-2.gog.com/abc", "isGame": True},
            {"id": 999, "title": "Some Movie", "image": "", "isGame": False, "isMovie": True},
        ],
    }
    resp = _resp(page_response)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        games = await gog_connector.fetch_owned_games("AT")
    assert len(games) == 1
    assert games[0].product_id == 1207660413
    assert games[0].title == "Shadowrun Returns"
    assert games[0].image_url == "https://images-2.gog.com/abc"


@pytest.mark.asyncio
async def test_fetch_owned_games_paginates_until_total_pages():
    page1 = {"page": 1, "totalPages": 2, "products": [{"id": 1, "title": "Game One", "image": "", "isGame": True}]}
    page2 = {"page": 2, "totalPages": 2, "products": [{"id": 2, "title": "Game Two", "image": "", "isGame": True}]}
    responses = [_resp(page1), _resp(page2)]
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", new=mock_get):
        games = await gog_connector.fetch_owned_games("AT")
    assert {g.product_id for g in games} == {1, 2}
    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_owned_games_raises_on_auth_failure():
    resp = _resp({}, status=401)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog_connector.GogAuthError):
            await gog_connector.fetch_owned_games("bad-token")


@pytest.mark.asyncio
async def test_fetch_owned_games_sends_bearer_header():
    resp = _resp({"page": 1, "totalPages": 1, "products": []})
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await gog_connector.fetch_owned_games("my-access-token")
    assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer my-access-token"
