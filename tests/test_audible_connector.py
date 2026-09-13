"""The login bridge is the genuinely novel part of this connector — see its
own module docstring on why Authenticator.from_login_external() (synchronous,
blocking, with an inline login_url_callback) needs a background thread +
queue to work at all from an HTTP request/response cycle. These tests
exercise the real thread/queue hand-off (not just the pure logic) against a
fake from_login_external, with a short poll-with-timeout since a real
background thread is involved.
"""

import time
from unittest.mock import patch

import httpx
import pytest

from app.connectors import audible_connector

_POLL_TIMEOUT_SECONDS = 2.0


@pytest.fixture(autouse=True)
def _reset_pending():
    audible_connector.clear_pending()
    yield
    audible_connector.clear_pending()


def _wait_until(predicate, timeout=_POLL_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_login_status_none_when_nothing_in_progress():
    assert audible_connector.login_status() is None


def test_login_result_none_when_nothing_in_progress():
    assert audible_connector.login_result() is None


def test_start_login_raises_when_already_in_progress():
    def fake_from_login_external(locale, login_url_callback=None, **kwargs):
        return login_url_callback("https://amazon.com/ap/signin?fake=1")  # blocks on the queue below

    with patch("audible.Authenticator.from_login_external", side_effect=fake_from_login_external):
        audible_connector.start_login("us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        with pytest.raises(audible_connector.AudibleLoginError):
            audible_connector.start_login("us")
        audible_connector.answer_login("https://amazon.com/ap/maplanding?openid.oa2.authorization_code=abc")


def test_login_url_prompt_surfaces_and_answer_completes_login():
    def fake_from_login_external(locale, login_url_callback=None, **kwargs):
        pasted = login_url_callback("https://amazon.com/ap/signin?fake=1")
        assert pasted == "https://amazon.com/ap/maplanding?openid.oa2.authorization_code=abc"
        return "fake-authenticator"

    with patch("audible.Authenticator.from_login_external", side_effect=fake_from_login_external):
        audible_connector.start_login("us")

        assert _wait_until(lambda: audible_connector.login_status() is not None)
        prompt = audible_connector.login_status()
        assert prompt.login_url == "https://amazon.com/ap/signin?fake=1"

        audible_connector.answer_login("https://amazon.com/ap/maplanding?openid.oa2.authorization_code=abc")

        assert _wait_until(lambda: audible_connector.login_result() is not None)
        auth, error = audible_connector.login_result()
        assert auth == "fake-authenticator"
        assert error is None


def test_start_login_passes_locale_and_login_url_callback():
    with patch("audible.Authenticator.from_login_external", return_value="fake-auth") as mock:
        audible_connector.start_login("uk")
        assert _wait_until(lambda: audible_connector.login_result() is not None)

    args, kwargs = mock.call_args
    assert args[0] == "uk"
    assert kwargs.get("login_url_callback") is not None


def test_start_login_patches_httpx_client_timeout_during_login_and_restores_it():
    # audible/login.py builds its own httpx.Client with no timeout override
    # at all, and from_login_external() (via the shared device-registration
    # step) exposes no way to pass one through — a real account hit "The
    # read operation timed out" against httpx's tight 5-second default on a
    # real (if slightly slower) homelab network path. This locks in both
    # that the patched client actually gets a longer timeout while
    # from_login_external is running, and that the patch is always undone
    # afterward regardless of outcome.
    original_client_cls = httpx.Client

    def fake_from_login_external(locale, login_url_callback=None, **kwargs):
        assert httpx.Client is not original_client_cls
        client = httpx.Client(base_url="https://example.com")
        try:
            assert client.timeout.read == audible_connector._LOGIN_HTTP_TIMEOUT_SECONDS
        finally:
            client.close()
        return login_url_callback("https://amazon.com/ap/signin?fake=1")

    with patch("audible.Authenticator.from_login_external", side_effect=fake_from_login_external):
        audible_connector.start_login("us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        audible_connector.answer_login("https://amazon.com/ap/maplanding?openid.oa2.authorization_code=abc")
        assert _wait_until(lambda: audible_connector.login_result() is not None)

    assert httpx.Client is original_client_cls


def test_start_login_patches_bare_httpx_post_and_get_too():
    # The *same* "read operation timed out" persisted even after the
    # httpx.Client patch above shipped — root cause was one level deeper:
    # audible/register.py's device-registration step (which runs right
    # after a successful browser login/paste-back) uses the bare
    # module-level httpx.post()/httpx.get() convenience functions, not
    # httpx.Client, so that patch alone never touched it.
    original_post = httpx.post
    original_get = httpx.get
    captured = {}

    def fake_from_login_external(locale, login_url_callback=None, **kwargs):
        assert httpx.post is not original_post
        assert httpx.get is not original_get
        with patch("httpx._api.request", return_value="posted") as mock_request:
            httpx.post("https://example.com/auth/register", json={})
            captured["post_kwargs"] = mock_request.call_args.kwargs
        return login_url_callback("https://amazon.com/ap/signin?fake=1")

    with patch("audible.Authenticator.from_login_external", side_effect=fake_from_login_external):
        audible_connector.start_login("us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        audible_connector.answer_login("https://amazon.com/ap/maplanding?openid.oa2.authorization_code=abc")
        assert _wait_until(lambda: audible_connector.login_result() is not None)

    assert captured["post_kwargs"].get("timeout") == audible_connector._LOGIN_HTTP_TIMEOUT_SECONDS
    assert httpx.post is original_post
    assert httpx.get is original_get


def test_httpx_patches_are_restored_even_on_failure():
    original_client_cls = httpx.Client
    original_post = httpx.post
    original_get = httpx.get
    with patch("audible.Authenticator.from_login_external", side_effect=ValueError("boom")):
        audible_connector.start_login("us")
        assert _wait_until(lambda: audible_connector.login_result() is not None)
    assert httpx.Client is original_client_cls
    assert httpx.post is original_post
    assert httpx.get is original_get


def test_login_failure_surfaces_as_an_error_not_a_result():
    with patch("audible.Authenticator.from_login_external", side_effect=ValueError("bad redirect url")):
        audible_connector.start_login("us")
        assert _wait_until(lambda: audible_connector.login_result() is not None)
        auth, error = audible_connector.login_result()
        assert auth is None
        assert error == "bad redirect url"


def test_clear_pending_resets_state():
    with patch("audible.Authenticator.from_login_external", return_value="fake-authenticator"):
        audible_connector.start_login("us")
        assert _wait_until(lambda: audible_connector.login_result() is not None)
    audible_connector.clear_pending()
    assert audible_connector.login_status() is None
    assert audible_connector.login_result() is None


@pytest.mark.asyncio
async def test_fetch_library_parses_real_shape():
    fake_response = {
        "items": [
            {
                "asin": "B001",
                "title": "A Great Book",
                "authors": [{"name": "Jane Author"}],
                "runtime_length_min": 605,
                "product_images": {"500": "https://example.com/cover.jpg"},
            },
            {"asin": "", "title": "Should Be Skipped"},
        ]
    }

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, path, params=None):
            assert path == "library"
            return fake_response

    with patch("audible.AsyncClient", return_value=_FakeAsyncClient()):
        books = await audible_connector.fetch_library(auth=object())

    assert len(books) == 1
    assert books[0].asin == "B001"
    assert books[0].title == "A Great Book"
    assert books[0].author == "Jane Author"
    assert books[0].runtime_minutes == 605
    assert books[0].cover_url == "https://example.com/cover.jpg"
