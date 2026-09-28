from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import asset_cache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # The failure cooldown is a module-level dict, not scoped to tmp_path —
    # without clearing it, a failure recorded by one test under a URL another
    # test reuses (several fixtures below share "https://example.invalid/...")
    # would suppress that later test's own fetch attempt.
    asset_cache._recent_failures.clear()


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


def test_extension_map_covers_bmp_and_avif():
    with patch("httpx.get", _fake_get(content_type="image/bmp")):
        bmp = asset_cache.get_or_fetch("ns", "https://example.invalid/no-ext-bmp")
    with patch("httpx.get", _fake_get(content_type="image/avif")):
        avif = asset_cache.get_or_fetch("ns", "https://example.invalid/no-ext-avif")
    assert bmp.suffix == ".bmp"
    assert avif.suffix == ".avif"


def test_a_200_response_that_is_not_an_image_is_not_cached():
    # The real bug this guards: a session/redirect hiccup can make the
    # "download" URL answer 200 with an HTML page instead of the image —
    # caching that as if it were the cover would be permanent and silent.
    html = b"<html><body>Sign in required</body></html>"
    with patch("httpx.get", _fake_get(content=html, content_type="text/html")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is None
    assert asset_cache.cached_path("ns", "https://example.invalid/art.jpg") is None


def test_real_image_bytes_are_accepted_even_with_a_missing_content_type():
    jpeg_magic = b"\xff\xd8\xff" + b"\x00" * 20
    with patch("httpx.get", _fake_get(content=jpeg_magic, content_type="")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is not None
    assert path.read_bytes() == jpeg_magic


def test_a_failed_fetch_is_not_retried_within_the_cooldown():
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")) as mock_get:
        asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    with patch("httpx.get") as mock_get_second:
        second = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")

    assert second is None
    mock_get_second.assert_not_called()
    assert mock_get.call_count == 1


def test_a_failure_in_one_namespace_does_not_suppress_another():
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        asset_cache.get_or_fetch("namespace-a", "https://example.invalid/art.jpg")
    mock_get = MagicMock(side_effect=_fake_get())
    with patch("httpx.get", mock_get):
        path = asset_cache.get_or_fetch("namespace-b", "https://example.invalid/art.jpg")

    assert path is not None
    mock_get.assert_called_once()


def test_evict_removes_the_cached_file_and_clears_the_cooldown():
    with patch("httpx.get", _fake_get()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path.exists()

    asset_cache.evict("ns", "https://example.invalid/art.jpg")

    assert not path.exists()
    assert asset_cache.cached_path("ns", "https://example.invalid/art.jpg") is None


def test_evict_lets_a_previously_failed_url_be_retried_immediately():
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")

    asset_cache.evict("ns", "https://example.invalid/art.jpg")

    mock_get = MagicMock(side_effect=_fake_get())
    with patch("httpx.get", mock_get):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is not None
    mock_get.assert_called_once()


def test_a_successful_fetch_leaves_no_leftover_temp_file():
    with patch("httpx.get", _fake_get()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    leftovers = [p for p in path.parent.iterdir() if p != path]
    assert leftovers == []


def test_refuses_to_fetch_a_private_ip_literal():
    with patch("httpx.get") as mock_get:
        path = asset_cache.get_or_fetch("ns", "http://169.254.169.254/latest/meta-data/")
    assert path is None
    mock_get.assert_not_called()


def test_refuses_to_fetch_a_non_http_scheme():
    with patch("httpx.get") as mock_get:
        path = asset_cache.get_or_fetch("ns", "ftp://example.invalid/art.jpg")
    assert path is None
    mock_get.assert_not_called()


def test_an_invalid_namespace_is_rejected_rather_than_silently_sanitized():
    for bad in ("../escape", "a/b", "a\\b", ""):
        with pytest.raises(ValueError):
            asset_cache.get_or_fetch(bad, "https://example.invalid/art.jpg")


def test_cached_path_picks_deterministically_among_stale_leftover_files():
    # Not expected in normal operation (get_or_fetch never leaves more than
    # one real file per key), but cached_path must still behave
    # deterministically rather than depend on filesystem iteration order if
    # it ever happens (e.g. a manual recovery after a disk issue).
    directory = asset_cache._cache_dir("ns")
    directory.mkdir(parents=True)
    key = asset_cache._cache_key("https://example.invalid/art.jpg")
    (directory / f"{key}.png").write_bytes(b"one")
    (directory / f"{key}.jpg").write_bytes(b"two")

    first = asset_cache.cached_path("ns", "https://example.invalid/art.jpg")
    second = asset_cache.cached_path("ns", "https://example.invalid/art.jpg")
    assert first == second


def test_a_real_hostname_is_still_fetchable():
    # The private-IP guard only catches a literal IP in the URL — a normal
    # hostname (which is what every real caller actually uses) is untouched.
    mock_get = MagicMock(side_effect=_fake_get())
    with patch("httpx.get", mock_get):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is not None
    mock_get.assert_called_once()
