from unittest.mock import patch

import httpx
import pytest

from app import asset_cache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)


def _fake_get(content=b"FAKE IMAGE BYTES", content_type="image/jpeg", status=200):
    def handler(url, **kwargs):
        return httpx.Response(status, content=content, headers={"content-type": content_type}, request=httpx.Request("GET", url))

    return handler


def test_cached_path_is_none_before_anything_fetched():
    assert asset_cache.cached_path("ns", "https://example.invalid/art.jpg") is None


def test_get_or_fetch_downloads_and_caches_on_first_call():
    with patch("httpx.get", _fake_get()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc")

    assert path is not None
    assert path.read_bytes() == b"FAKE IMAGE BYTES"
    assert path.suffix == ".jpg"


def test_get_or_fetch_does_not_refetch_on_a_second_call():
    with patch("httpx.get", _fake_get()) as mock_get:
        first = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc")
    with patch("httpx.get") as mock_get_second:
        second = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc")

    assert first == second
    mock_get_second.assert_not_called()


def test_two_urls_with_the_same_path_but_different_signatures_share_one_cache_entry():
    # This is the whole point: a presigned URL's signature/expiry changes on
    # every fresh sync even though the underlying image hasn't — caching by
    # host+path (not the full URL) means a later sync's differently-signed
    # URL for the same image reuses what's already on disk instead of
    # re-fetching or creating a duplicate entry.
    with patch("httpx.get", _fake_get()):
        first = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc&e=111")
    with patch("httpx.get") as mock_get_second:
        second = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=xyz&e=222")

    assert first == second
    mock_get_second.assert_not_called()


def test_a_different_path_gets_its_own_cache_entry():
    with patch("httpx.get", _fake_get(content=b"IMAGE ONE")):
        one = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg")
    with patch("httpx.get", _fake_get(content=b"IMAGE TWO")):
        two = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A2/art.jpg")

    assert one != two
    assert one.read_bytes() == b"IMAGE ONE"
    assert two.read_bytes() == b"IMAGE TWO"


def test_different_namespaces_stay_separate():
    with patch("httpx.get", _fake_get()):
        a = asset_cache.get_or_fetch("namespace-a", "https://example.invalid/art.jpg")
        b = asset_cache.get_or_fetch("namespace-b", "https://example.invalid/art.jpg")

    assert a != b
    assert a.parent.name == "namespace-a"
    assert b.parent.name == "namespace-b"


def test_get_or_fetch_returns_none_for_a_blank_url():
    assert asset_cache.get_or_fetch("ns", "") is None


def test_get_or_fetch_returns_none_on_a_failed_request():
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        assert asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg") is None


def test_get_or_fetch_returns_none_on_a_4xx_response():
    with patch("httpx.get", _fake_get(status=404)):
        assert asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg") is None


def test_extension_falls_back_to_content_type_when_the_url_has_none():
    with patch("httpx.get", _fake_get(content_type="image/png")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/no-extension-here")
    assert path.suffix == ".png"
