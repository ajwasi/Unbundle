"""The login bridge is the genuinely novel part of this connector — see its
own module docstring on why Authenticator.from_login() (synchronous,
blocking, with inline callbacks) needs a background thread + queue to work
at all from an HTTP request/response cycle. These tests exercise the real
thread/queue hand-off (not just the pure logic) against a fake from_login,
with a short poll-with-timeout since a real background thread is involved.
"""

import time
from unittest.mock import patch

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
    def fake_from_login(username, password, locale, captcha_callback=None, otp_callback=None, **kwargs):
        return otp_callback()  # blocks on the queue below until answered

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        with pytest.raises(audible_connector.AudibleLoginError):
            audible_connector.start_login("user", "pass", "us")
        audible_connector.answer_login("123456")  # let the background thread finish


def test_captcha_prompt_surfaces_and_answer_completes_login():
    def fake_from_login(username, password, locale, captcha_callback=None, otp_callback=None, **kwargs):
        answer = captcha_callback("https://example.com/captcha.jpg")
        assert answer == "solved"
        return "fake-authenticator"

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        audible_connector.start_login("user", "pass", "us")

        assert _wait_until(lambda: audible_connector.login_status() is not None)
        prompt = audible_connector.login_status()
        assert prompt.kind == "captcha"
        assert prompt.prompt == "https://example.com/captcha.jpg"

        audible_connector.answer_login("solved")

        assert _wait_until(lambda: audible_connector.login_result() is not None)
        auth, error = audible_connector.login_result()
        assert auth == "fake-authenticator"
        assert error is None


def test_otp_prompt_has_no_url():
    def fake_from_login(username, password, locale, captcha_callback=None, otp_callback=None, **kwargs):
        return otp_callback()

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        prompt = audible_connector.login_status()
        assert prompt.kind == "otp"
        assert prompt.prompt == ""
        audible_connector.answer_login("000000")


def test_login_failure_surfaces_as_an_error_not_a_result():
    with patch("audible.Authenticator.from_login", side_effect=ValueError("bad credentials")):
        audible_connector.start_login("user", "wrong-pass", "us")
        assert _wait_until(lambda: audible_connector.login_result() is not None)
        auth, error = audible_connector.login_result()
        assert auth is None
        assert error == "bad credentials"


def test_clear_pending_resets_state():
    with patch("audible.Authenticator.from_login", return_value="fake-authenticator"):
        audible_connector.start_login("user", "pass", "us")
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
