import contextlib
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


def _fake_stream(content=b"FAKE IMAGE BYTES", content_type="image/jpeg", status=200, headers=None):
    """Stands in for asset_cache._client.stream — a context manager yielding
    an httpx.Response, matching exactly how _fetch_validated uses the real
    client (`with _client.stream("GET", url) as resp:`).
    """

    @contextlib.contextmanager
    def handler(method, url, **kwargs):
        h = {"content-type": content_type}
        if headers:
            h.update(headers)
        yield httpx.Response(status, content=content, headers=h, request=httpx.Request(method, url))

    return handler


def _stream_mock(*args, **kwargs):
    """Same as _fake_stream, wrapped in a MagicMock so call-count assertions
    work — plain functions used as a patch replacement aren't Mocks."""
    return MagicMock(side_effect=_fake_stream(*args, **kwargs))


def _redirect_then(final_content=b"FINAL IMAGE BYTES", final_content_type="image/jpeg", location="https://example.invalid/final.jpg", redirects=1):
    """A stream handler returning `redirects` 302s before finally answering
    with the real content — for exercising _fetch_validated's manual
    redirect-following.
    """
    calls = {"n": 0}

    @contextlib.contextmanager
    def handler(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] <= redirects:
            yield httpx.Response(302, headers={"location": location}, request=httpx.Request(method, url))
        else:
            yield httpx.Response(
                200, content=final_content, headers={"content-type": final_content_type}, request=httpx.Request(method, url)
            )

    return handler


def _always_redirect(location="https://example.invalid/loop.jpg"):
    @contextlib.contextmanager
    def handler(method, url, **kwargs):
        yield httpx.Response(302, headers={"location": location}, request=httpx.Request(method, url))

    return handler


def test_cached_path_is_none_before_anything_fetched():
    assert asset_cache.cached_path("ns", "https://example.invalid/art.jpg") is None


def test_get_or_fetch_downloads_and_caches_on_first_call():
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc")

    assert path is not None
    assert path.read_bytes() == b"FAKE IMAGE BYTES"
    assert path.suffix == ".jpg"


def test_get_or_fetch_does_not_refetch_on_a_second_call():
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        first = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc")
    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        second = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc")

    assert first == second
    mock_stream.assert_not_called()


def test_two_urls_with_the_same_path_but_different_signatures_share_one_cache_entry():
    # This is the whole point: a presigned URL's signature/expiry changes on
    # every fresh sync even though the underlying image hasn't — caching by
    # host+path (not the full URL) means a later sync's differently-signed
    # URL for the same image reuses what's already on disk instead of
    # re-fetching or creating a duplicate entry.
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        first = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=abc&e=111")
    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        second = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg?sig=xyz&e=222")

    assert first == second
    mock_stream.assert_not_called()


def test_a_different_path_gets_its_own_cache_entry():
    with patch.object(asset_cache._client, "stream", _fake_stream(content=b"IMAGE ONE")):
        one = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A1/art.jpg")
    with patch.object(asset_cache._client, "stream", _fake_stream(content=b"IMAGE TWO")):
        two = asset_cache.get_or_fetch("ns", "https://example.invalid/albums/A2/art.jpg")

    assert one != two
    assert one.read_bytes() == b"IMAGE ONE"
    assert two.read_bytes() == b"IMAGE TWO"


def test_different_namespaces_stay_separate():
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        a = asset_cache.get_or_fetch("namespace-a", "https://example.invalid/art.jpg")
        b = asset_cache.get_or_fetch("namespace-b", "https://example.invalid/art.jpg")

    assert a != b
    assert a.parent.name == "namespace-a"
    assert b.parent.name == "namespace-b"


def test_get_or_fetch_returns_none_for_a_blank_url():
    assert asset_cache.get_or_fetch("ns", "") is None


def test_get_or_fetch_returns_none_on_a_failed_request():
    def handler(method, url, **kwargs):
        raise httpx.ConnectError("refused")

    with patch.object(asset_cache._client, "stream", handler):
        assert asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg") is None


def test_get_or_fetch_returns_none_on_a_4xx_response():
    with patch.object(asset_cache._client, "stream", _fake_stream(status=404)):
        assert asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg") is None


def test_extension_falls_back_to_content_type_when_the_url_has_none():
    with patch.object(asset_cache._client, "stream", _fake_stream(content_type="image/png")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/no-extension-here")
    assert path.suffix == ".png"


def test_extension_map_covers_bmp_and_avif_when_content_type_says_so():
    with patch.object(asset_cache._client, "stream", _fake_stream(content_type="image/bmp")):
        bmp = asset_cache.get_or_fetch("ns", "https://example.invalid/no-ext-bmp")
    with patch.object(asset_cache._client, "stream", _fake_stream(content_type="image/avif")):
        avif = asset_cache.get_or_fetch("ns", "https://example.invalid/no-ext-avif")
    assert bmp.suffix == ".bmp"
    assert avif.suffix == ".avif"


def test_a_200_response_that_is_not_an_image_is_not_cached():
    # The real bug this guards: a session/redirect hiccup can make the
    # "download" URL answer 200 with an HTML page instead of the image —
    # caching that as if it were the cover would be permanent and silent.
    html = b"<html><body>Sign in required</body></html>"
    with patch.object(asset_cache._client, "stream", _fake_stream(content=html, content_type="text/html")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is None
    assert asset_cache.cached_path("ns", "https://example.invalid/art.jpg") is None


def test_real_jpeg_bytes_are_accepted_even_with_a_missing_content_type():
    jpeg_magic = b"\xff\xd8\xff" + b"\x00" * 20
    with patch.object(asset_cache._client, "stream", _fake_stream(content=jpeg_magic, content_type="")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is not None
    assert path.read_bytes() == jpeg_magic


def test_real_avif_bytes_are_accepted_even_with_a_missing_content_type():
    avif_magic = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 20
    with patch.object(asset_cache._client, "stream", _fake_stream(content=avif_magic, content_type="")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art")
    assert path is not None


def test_bmps_weak_two_byte_signature_is_not_trusted_without_a_content_type():
    # "BM" is BMP's whole magic number — genuinely only 2 bytes, unlike every
    # other format here — so it's deliberately not used to justify caching a
    # response on its own. A real BMP still works fine via a correct
    # content-type (see test_extension_map_covers_bmp_and_avif_when_content_type_says_so).
    bmp_ish = b"BM" + b"\x00" * 20
    with patch.object(asset_cache._client, "stream", _fake_stream(content=bmp_ish, content_type="")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art")
    assert path is None


def test_svg_is_rejected_even_with_a_correct_content_type():
    # SVG can embed <script> — this cache is explicitly built for reuse
    # beyond today's <img>-only callers, so it doesn't carry that format at
    # all rather than assume every future caller renders it as safely.
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    with patch.object(asset_cache._client, "stream", _fake_stream(content=svg, content_type="image/svg+xml")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.svg")
    assert path is None


def test_a_failed_fetch_is_not_retried_within_the_cooldown():
    mock_stream = MagicMock(side_effect=httpx.ConnectError("refused"))
    with patch.object(asset_cache._client, "stream", mock_stream):
        asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    mock_stream_2 = MagicMock(side_effect=httpx.ConnectError("refused"))
    with patch.object(asset_cache._client, "stream", mock_stream_2):
        second = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")

    assert second is None
    mock_stream_2.assert_not_called()


def test_a_failure_in_one_namespace_does_not_suppress_another():
    with patch.object(asset_cache._client, "stream", MagicMock(side_effect=httpx.ConnectError("refused"))):
        asset_cache.get_or_fetch("namespace-a", "https://example.invalid/art.jpg")
    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        path = asset_cache.get_or_fetch("namespace-b", "https://example.invalid/art.jpg")

    assert path is not None
    mock_stream.assert_called_once()


def test_evict_removes_the_cached_file_and_clears_the_cooldown():
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path.exists()

    asset_cache.evict("ns", "https://example.invalid/art.jpg")

    assert not path.exists()
    assert asset_cache.cached_path("ns", "https://example.invalid/art.jpg") is None


def test_evict_accepts_an_optional_reason_with_no_behavior_change():
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    asset_cache.evict("ns", "https://example.invalid/art.jpg", reason="user requested a refresh")
    assert not path.exists()


def test_evict_lets_a_previously_failed_url_be_retried_immediately():
    with patch.object(asset_cache._client, "stream", MagicMock(side_effect=httpx.ConnectError("refused"))):
        asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")

    asset_cache.evict("ns", "https://example.invalid/art.jpg")

    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is not None
    mock_stream.assert_called_once()


def test_a_successful_fetch_leaves_no_leftover_temp_file():
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    leftovers = [p for p in path.parent.iterdir() if p != path]
    assert leftovers == []


def test_an_os_error_during_write_is_treated_as_a_failed_fetch(monkeypatch):
    from pathlib import Path

    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    with patch.object(asset_cache._client, "stream", _fake_stream()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is None


def test_refuses_to_fetch_a_private_ip_literal():
    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        path = asset_cache.get_or_fetch("ns", "http://169.254.169.254/latest/meta-data/")
    assert path is None
    mock_stream.assert_not_called()


def test_refuses_an_ipv4_mapped_ipv6_loopback_literal():
    # ::ffff:127.0.0.1 parses as an IPv6Address, which is never "in" any of
    # the plain IPv4Network private ranges by construction — a real bypass
    # if not unwrapped and checked separately.
    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        path = asset_cache.get_or_fetch("ns", "http://[::ffff:127.0.0.1]/secret")
    assert path is None
    mock_stream.assert_not_called()


def test_refuses_to_fetch_a_non_http_scheme():
    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        path = asset_cache.get_or_fetch("ns", "ftp://example.invalid/art.jpg")
    assert path is None
    mock_stream.assert_not_called()


def test_a_real_hostname_is_still_fetchable():
    # The private-IP guard only catches a literal IP in the URL — a normal
    # hostname (which is what every real caller actually uses) is untouched.
    mock_stream = _stream_mock()
    with patch.object(asset_cache._client, "stream", mock_stream):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/art.jpg")
    assert path is not None
    mock_stream.assert_called_once()


def test_a_redirect_to_a_safe_host_is_followed():
    with patch.object(asset_cache._client, "stream", _redirect_then(final_content=b"REDIRECTED IMAGE")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/start.jpg")
    assert path is not None
    assert path.read_bytes() == b"REDIRECTED IMAGE"


def test_a_redirect_to_a_private_ip_is_not_followed():
    # The core fix: httpx's own follow_redirects=True only validates the URL
    # the caller originally passed in — a "safe" host redirecting to a
    # private address would otherwise sail straight through.
    handler = _redirect_then(location="http://169.254.169.254/latest/meta-data/")
    with patch.object(asset_cache._client, "stream", handler):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/start.jpg")
    assert path is None


def test_a_relative_redirect_location_is_resolved_against_the_current_url():
    handler = _redirect_then(location="/final.jpg", final_content=b"RELATIVE REDIRECT IMAGE")
    with patch.object(asset_cache._client, "stream", handler):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/start.jpg")
    assert path is not None
    assert path.read_bytes() == b"RELATIVE REDIRECT IMAGE"


def test_too_many_redirects_gives_up_rather_than_looping_forever():
    with patch.object(asset_cache._client, "stream", _always_redirect()):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/start.jpg")
    assert path is None


def test_a_response_over_the_size_cap_is_rejected():
    huge = b"x" * (asset_cache._MAX_RESPONSE_BYTES + 1)
    with patch.object(asset_cache._client, "stream", _fake_stream(content=huge)):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/huge.jpg")
    assert path is None
    assert asset_cache.cached_path("ns", "https://example.invalid/huge.jpg") is None


def test_a_response_right_at_the_size_cap_is_accepted():
    exactly = b"\xff\xd8\xff" + b"x" * (asset_cache._MAX_RESPONSE_BYTES - 3)
    with patch.object(asset_cache._client, "stream", _fake_stream(content=exactly, content_type="")):
        path = asset_cache.get_or_fetch("ns", "https://example.invalid/exact.jpg")
    assert path is not None


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


def test_safe_log_url_strips_the_query_string():
    scrubbed = asset_cache._safe_log_url("https://example.invalid/art.jpg?X-Amz-Signature=SECRET123")
    assert "SECRET123" not in scrubbed
    assert scrubbed == "https://example.invalid/art.jpg"


def test_safe_log_url_does_not_raise_on_garbage_input():
    assert asset_cache._safe_log_url("not a url at all") is not None


def test_failed_fetches_are_pruned_once_the_cap_is_hit():
    import time as time_module

    asset_cache._recent_failures.clear()
    now = time_module.monotonic()
    # Fill past the cap with old entries, oldest first.
    for i in range(asset_cache._MAX_FAILURE_ENTRIES + 1):
        asset_cache._recent_failures[f"ns::key{i}"] = now - (asset_cache._MAX_FAILURE_ENTRIES - i)

    asset_cache._record_failure("ns::newest")

    assert len(asset_cache._recent_failures) <= asset_cache._MAX_FAILURE_ENTRIES
    assert "ns::newest" in asset_cache._recent_failures  # the just-added entry always survives its own insertion
