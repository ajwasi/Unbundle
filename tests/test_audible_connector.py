"""The login bridge is the genuinely novel part of this connector — see its
own module docstring on why Authenticator.from_login() (synchronous,
blocking, with inline callbacks) needs a background thread + queue to work
at all from an HTTP request/response cycle. These tests exercise the real
thread/queue hand-off (not just the pure logic) against a fake from_login,
with a short poll-with-timeout since a real background thread is involved.
"""

import time
from unittest.mock import patch

import audible.login
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


def test_cvf_prompt_has_no_url():
    # Regression guard: a real account hit exactly this path in production —
    # from_login must always be given all four callbacks (captcha, otp, cvf,
    # approval), or audible falls back to its own console input() default,
    # which raises "EOF when reading a line" with no attached terminal.
    def fake_from_login(username, password, locale, cvf_callback=None, **kwargs):
        return cvf_callback()

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        prompt = audible_connector.login_status()
        assert prompt.kind == "cvf"
        assert prompt.prompt == ""
        audible_connector.answer_login("123456")


def test_check_for_cvf_diagnostic_logs_page_text_and_restores(caplog):
    # cvf_callback() itself gets zero context from audible (confirmed via
    # inspect.signature) — this is the only way to see what Amazon actually
    # told a real account at this step, since nothing never received a code.
    original_check_for_cvf = audible.login.check_for_cvf

    class _FakeTag:
        def get_text(self, *args, **kwargs):
            return "We sent a code to your phone ending in 1234"

    class _FakeSoup:
        def find(self, *args, **kwargs):
            return _FakeTag()

    def fake_from_login(username, password, locale, cvf_callback=None, **kwargs):
        assert audible.login.check_for_cvf is not original_check_for_cvf
        assert audible.login.check_for_cvf(_FakeSoup()) is True
        return cvf_callback()

    with caplog.at_level("INFO", logger="app.connectors.audible_connector"):
        with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
            audible_connector.start_login("user", "pass", "us")
            assert _wait_until(lambda: audible_connector.login_status() is not None)
            audible_connector.answer_login("123456")
            assert _wait_until(lambda: audible_connector.login_result() is not None)

    assert audible.login.check_for_cvf is original_check_for_cvf
    assert "We sent a code to your phone ending in 1234" in caplog.text


def test_approval_prompt_completes_on_any_answer():
    def fake_from_login(username, password, locale, approval_callback=None, **kwargs):
        approval_callback()
        return "fake-authenticator"

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        prompt = audible_connector.login_status()
        assert prompt.kind == "approval"

        audible_connector.answer_login("approved")

        assert _wait_until(lambda: audible_connector.login_result() is not None)
        auth, error = audible_connector.login_result()
        assert auth == "fake-authenticator"
        assert error is None


def test_start_login_always_passes_all_four_callbacks():
    # The actual bug: an earlier version only passed captcha_callback/
    # otp_callback, so any account needing a CVF or approval step fell
    # through to audible's own input()-based default inside this container.
    with patch("audible.Authenticator.from_login", return_value="fake-auth") as mock_from_login:
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_result() is not None)

    kwargs = mock_from_login.call_args.kwargs
    for name in ("captcha_callback", "otp_callback", "cvf_callback", "approval_callback"):
        assert kwargs.get(name) is not None, f"{name} was not passed to from_login"


def test_start_login_patches_httpx_client_timeout_during_login_and_restores_it():
    # audible/login.py builds its own httpx.Client with no timeout override
    # at all, and from_login() exposes no way to pass one through — a real
    # account hit "The read operation timed out" against httpx's tight
    # 5-second default on a real (if slightly slower) homelab network path.
    # This locks in both that the patched client actually gets a longer
    # timeout while from_login is running, and that the patch is always
    # undone afterward regardless of outcome.
    original_client_cls = httpx.Client

    def fake_from_login(username, password, locale, otp_callback=None, **kwargs):
        assert httpx.Client is not original_client_cls
        client = httpx.Client(base_url="https://example.com")
        try:
            assert client.timeout.read == audible_connector._LOGIN_HTTP_TIMEOUT_SECONDS
        finally:
            client.close()
        return otp_callback()

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        audible_connector.answer_login("000000")
        assert _wait_until(lambda: audible_connector.login_result() is not None)

    assert httpx.Client is original_client_cls


def test_start_login_patches_bare_httpx_post_and_get_too():
    # The *same* "read operation timed out" persisted even after the
    # httpx.Client patch above shipped — root cause was one level deeper:
    # audible/register.py's device-registration step (which runs right
    # after the interactive captcha/otp/cvf/approval part succeeds) uses the
    # bare module-level httpx.post()/httpx.get() convenience functions, not
    # httpx.Client, so that patch alone never touched it.
    original_post = httpx.post
    original_get = httpx.get
    captured = {}

    def fake_from_login(username, password, locale, otp_callback=None, **kwargs):
        assert httpx.post is not original_post
        assert httpx.get is not original_get
        with patch("httpx._api.request", return_value="posted") as mock_request:
            httpx.post("https://example.com/auth/register", json={})
            captured["post_kwargs"] = mock_request.call_args.kwargs
        return otp_callback()

    with patch("audible.Authenticator.from_login", side_effect=fake_from_login):
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_status() is not None)
        audible_connector.answer_login("000000")
        assert _wait_until(lambda: audible_connector.login_result() is not None)

    assert captured["post_kwargs"].get("timeout") == audible_connector._LOGIN_HTTP_TIMEOUT_SECONDS
    assert httpx.post is original_post
    assert httpx.get is original_get


def test_httpx_patches_are_restored_even_on_failure():
    original_client_cls = httpx.Client
    original_post = httpx.post
    original_get = httpx.get
    with patch("audible.Authenticator.from_login", side_effect=ValueError("boom")):
        audible_connector.start_login("user", "pass", "us")
        assert _wait_until(lambda: audible_connector.login_result() is not None)
    assert httpx.Client is original_client_cls
    assert httpx.post is original_post
    assert httpx.get is original_get


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
