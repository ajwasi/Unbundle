import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.connectors import chirp_connector as chirp

FIXTURE = Path(__file__).parent / "fixtures" / "chirp_library_response.json"


def _library_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]


# ------------------------------------------------------------- page scraping

def test_parse_csrf_token_from_meta_tag():
    html = '<meta name="csrf-token" content="abc123" />'
    assert chirp.parse_csrf_token(html) == "abc123"


def test_parse_csrf_token_from_hidden_form_field():
    html = '<input type="hidden" name="authenticity_token" value="def456" />'
    assert chirp.parse_csrf_token(html) == "def456"


def test_parse_csrf_token_prefers_meta_tag_when_both_present():
    html = (
        '<meta name="csrf-token" content="meta-token" />'
        '<input type="hidden" name="authenticity_token" value="form-token" />'
    )
    assert chirp.parse_csrf_token(html) == "meta-token"


def test_parse_csrf_token_returns_none_when_absent():
    assert chirp.parse_csrf_token("<html></html>") is None


def test_parse_user_id():
    html = '<div data=\'{"user":{"email":"a@b.com","userId":4210139}}\'></div>'
    assert chirp.parse_user_id(html) == 4210139


def test_parse_user_id_returns_none_when_absent():
    assert chirp.parse_user_id("<html></html>") is None


def test_parse_decryption_key():
    html = '<div class="user-audiobook" data-audiobook-id="626000" data-dk="somekeyvalue"></div>'
    assert chirp.parse_decryption_key(html) == "somekeyvalue"


def test_parse_audiobook_id_from_player_page():
    html = '<div class="user-audiobook" data-audiobook-id="626000" data-dk="somekeyvalue"></div>'
    assert chirp.parse_audiobook_id_from_player_page(html) == "626000"


# --------------------------------------------------------------------- auth

async def test_login_posts_the_confirmed_form_shape_with_a_scraped_csrf_token():
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == chirp.SIGN_IN_URL:
            return httpx.Response(200, text='<meta name="csrf-token" content="freshtoken" />')
        if request.method == "POST" and str(request.url) == chirp.SIGN_IN_URL:
            posted["body"] = request.content.decode("utf-8")
            # A real successful login redirects away from /users/sign_in —
            # simulated here as httpx would actually see it after following
            # the redirect, so resp.url ends up somewhere else entirely
            # (what the connector's own success/failure check relies on).
            return httpx.Response(303, headers={"location": "/home"})
        if request.method == "GET" and str(request.url) == f"{chirp.BASE_URL}/home":
            return httpx.Response(200, text="<html>My Library</html>")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await chirp.login(client, "a@b.com", "hunter2")

    assert "authenticity_token=freshtoken" in posted["body"]
    assert "user%5Bemail%5D=a%40b.com" in posted["body"]
    assert "user%5Bpassword%5D=hunter2" in posted["body"]
    assert "user%5Bremember_me%5D=1" in posted["body"]


async def test_login_raises_when_no_csrf_token_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>no token here</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(chirp.ChirpAuthError):
        await chirp.login(client, "a@b.com", "hunter2")


async def test_login_raises_a_specific_error_when_cloudflare_intercepts_the_get():
    # The single biggest real-world failure mode this connector was always
    # expected to hit (see the module's own docstring) — confirmed live
    # against a real account: distinguished from a generic "page changed"
    # failure so the error actually says what happened.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Just a moment...</body></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(chirp.ChirpAuthError, match="Cloudflare"):
        await chirp.login(client, "a@b.com", "hunter2")


async def test_looks_like_cloudflare_challenge_true_and_false_cases():
    assert chirp._looks_like_cloudflare_challenge("<html>Just a moment...</html>") is True
    assert chirp._looks_like_cloudflare_challenge('<script src="/cdn-cgi/challenge-platform/x.js">') is True
    assert chirp._looks_like_cloudflare_challenge('<meta name="csrf-token" content="abc">') is False


async def test_login_follows_a_redirect_on_the_initial_get():
    # A real, independently-plausible cause of "no CSRF token found" that
    # has nothing to do with Cloudflare: without following redirects, a GET
    # that gets redirected (canonical URL, locale prefix, anything) comes
    # back as the bare 3xx itself with no page to find a token in at all.
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.method == "GET" and str(request.url) == chirp.SIGN_IN_URL:
            return httpx.Response(301, headers={"location": f"{chirp.BASE_URL}/users/sign_in/"})
        if request.method == "GET":
            return httpx.Response(200, text='<meta name="csrf-token" content="freshtoken" />')
        posted_body = request.content.decode("utf-8")
        assert "authenticity_token=freshtoken" in posted_body
        return httpx.Response(303, headers={"location": "/home"})

    def home_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>My Library</html>")

    def combined(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{chirp.BASE_URL}/home":
            return home_handler(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(combined))
    await chirp.login(client, "a@b.com", "hunter2")
    assert f"{chirp.BASE_URL}/users/sign_in/" in calls  # the redirect target was actually fetched


async def test_login_raises_on_devises_own_rejected_login_message():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text='<meta name="csrf-token" content="t" />')
        # A rejected login re-renders the sign-in form with a 200 (not a
        # redirect) — Devise's own default flash copy, not yet confirmed
        # against a real failed attempt (see the connector's own docstring).
        return httpx.Response(200, text="<html>Invalid Email or password.</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(chirp.ChirpAuthError):
        await chirp.login(client, "a@b.com", "wrongpassword")


async def test_login_raises_when_redirected_back_to_sign_in():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text='<meta name="csrf-token" content="t" />')
        # Redirects back to the same sign-in URL rather than somewhere new —
        # the other shape a rejected login could plausibly take.
        return httpx.Response(303, headers={"location": chirp.SIGN_IN_URL})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(chirp.ChirpAuthError):
        await chirp.login(client, "a@b.com", "wrongpassword")


# ------------------------------------------------------------------ GraphQL

def test_parse_library_page_against_a_real_captured_response():
    items, total = chirp.parse_library_page(_library_payload())

    assert total == 78
    assert len(items) == 3
    wool = items[0]
    assert wool.purchase_id == "27991647"
    assert wool.audiobook_id == "626000"
    assert wool.title == "Wool"
    assert wool.authors == "Hugh Howey"
    assert wool.narrators == "Edoardo Ballerini"
    assert wool.url_path == "/audiobooks/wool-by-hugh-howey-4415b4a1b5"
    assert wool.cover_url.startswith("https://img.chirpbooks.com/")
    assert wool.progress_status == "IN_PROGRESS"
    assert wool.position_percent == 14
    assert wool.playable is True
    assert wool.series_name == "The Silo Saga"
    assert wool.series_number == "1"


def test_parse_library_page_handles_a_book_with_no_series():
    items, _ = chirp.parse_library_page(_library_payload())
    space_holes = next(i for i in items if i.title == "Space Holes")
    # Has a series in the real payload — check one with series=None instead
    # by constructing a minimal payload directly.
    payload = {
        "currentUserAudiobooks": [
            {
                "id": "1",
                "progressStatus": "NOT_STARTED",
                "positionPercent": 0,
                "playable": True,
                "audiobook": {
                    "id": "2",
                    "url": "/audiobooks/standalone",
                    "coverUrl": "https://img.chirpbooks.com/x.jpg",
                    "displayTitle": "Standalone Book",
                    "displayAuthors": "Some Author",
                    "displayNarrators": "Some Narrator",
                    "seriesAudiobook": None,
                },
            }
        ],
        "currentUserAudiobooksCount": 1,
    }
    items, total = chirp.parse_library_page(payload)
    assert items[0].series_name is None
    assert items[0].series_number is None
    assert space_holes.title == "Space Holes"  # sanity check the fixture-based lookup itself worked


def test_parse_library_page_handles_an_empty_list():
    items, total = chirp.parse_library_page({"currentUserAudiobooks": [], "currentUserAudiobooksCount": 0})
    assert items == []
    assert total == 0


async def test_fetch_library_page_sends_the_reconstructed_query_and_parses_the_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == chirp.GRAPHQL_URL
        body = json.loads(request.content)
        assert body["operationName"] == "fetchCurrentUserAudiobooks"
        assert body["variables"] == {"page": 1, "perPage": 20}
        return httpx.Response(200, json={"data": _library_payload()})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    items, total = await chirp.fetch_library_page(client)
    assert total == 78
    assert len(items) == 3


async def test_graphql_raises_on_an_error_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "something broke"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(chirp.ChirpRequestError):
        await chirp._graphql(client, "op", "query", {})


async def test_graphql_raises_a_specific_error_when_cloudflare_intercepts_the_post():
    # A stale/expired pasted cookie session is expected to fail exactly this
    # way: Cloudflare answers instead of Chirp's real API ever seeing the
    # request, so there's no JSON to parse at all.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Just a moment...</body></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(chirp.ChirpRequestError, match="Cloudflare"):
        await chirp._graphql(client, "op", "query", {})


async def test_graphql_raises_a_generic_error_on_other_non_json_responses():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Unexpected maintenance page</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(chirp.ChirpRequestError, match="did not return JSON"):
        await chirp._graphql(client, "op", "query", {})


def test_parse_tracks():
    payload = {
        "audiobook": {
            "tracks": [
                {"partNumber": 1, "chapterNumber": 1, "offsetFromBookStartMs": 0, "durationMs": 600000, "displayName": "Chapter 1"},
                {"partNumber": 1, "chapterNumber": 2, "offsetFromBookStartMs": 600000, "durationMs": 500000, "displayName": "Chapter 2"},
            ]
        }
    }
    tracks = chirp.parse_tracks(payload)
    assert len(tracks) == 2
    assert tracks[0].display_name == "Chapter 1"
    assert tracks[1].offset_from_book_start_ms == 600000


def test_parse_tracks_handles_missing_audiobook():
    assert chirp.parse_tracks({}) == []


async def test_fetch_tracks_sends_the_confirmed_query_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["operationName"] == "fetchAudiobookTracks"
        assert body["variables"] == {"id": "626000"}
        return httpx.Response(
            200,
            json={"data": {"audiobook": {"tracks": [
                {"partNumber": 1, "chapterNumber": 1, "offsetFromBookStartMs": 0, "durationMs": 1000, "displayName": "Ch1"}
            ]}}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tracks = await chirp.fetch_tracks(client, "626000")
    assert tracks[0].display_name == "Ch1"


async def test_fetch_encrypted_track_url_sends_the_confirmed_query_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["operationName"] == "fetchAudiobookTrackUrl"
        assert body["variables"] == {"id": "626000", "partNumber": 1, "chapterNumber": 2}
        return httpx.Response(200, json={"data": {"audiobook": {"track": {"webPlayerMediaUrl": "ZW5jcnlwdGVk"}}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    url = await chirp.fetch_encrypted_track_url(client, "626000", 1, 2)
    assert url == "ZW5jcnlwdGVk"


# --------------------------------------------------------------- decryption

def test_derive_iv_matches_the_confirmed_padding_scheme():
    # str(4210139).rjust(12, "x") == "xxxxx4210139" — the real account id
    # from this integration's own captures, computed independently here
    # rather than copied from the connector to actually exercise the logic.
    expected = base64.b64encode(b"xxxxx4210139")
    assert chirp.derive_iv(4210139) == expected
    assert len(chirp.derive_iv(4210139)) == 16  # exactly one AES block


def test_derive_iv_handles_a_12_digit_id_with_no_padding_needed():
    twelve_digits = 123456789012
    iv = chirp.derive_iv(twelve_digits)
    assert base64.b64decode(iv) == b"123456789012"


def test_decrypt_track_url_roundtrips_against_a_real_aes_cbc_encryption():
    key = b"0123456789abcdef"  # 16-byte AES-128 key
    iv = chirp.derive_iv(4210139)
    plaintext = b"https://cdn.example.invalid/track.mp3\n"  # trailing byte stripped by the connector, matching the reference behavior

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    # AES-CBC needs block-aligned input — pad to a 16-byte boundary the same
    # blunt way the reference implementation's own encoder must, since this
    # test only needs a round-trippable ciphertext, not a byte-exact replica
    # of Chirp's own padding scheme.
    padded = plaintext + b"\x00" * (-len(plaintext) % 16 or 16)
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")

    decrypted = chirp.decrypt_track_url(ciphertext_b64, key, iv)
    assert decrypted.startswith("https://cdn.example.invalid/track.mp3")


# ------------------------------------------------------ fetch_library_preview

async def test_fetch_library_preview_logs_in_then_fetches_the_page():
    calls = []

    async def fake_login(client, email, password):
        calls.append(("login", email, password))

    async def fake_fetch(client, page, per_page):
        calls.append(("fetch", page, per_page))
        return [], 78

    with patch.object(chirp, "login", new=fake_login):
        with patch.object(chirp, "fetch_library_page", new=fake_fetch):
            books, total = await chirp.fetch_library_preview("a@b.com", "hunter2", page=2, per_page=10)

    assert calls == [("login", "a@b.com", "hunter2"), ("fetch", 2, 10)]
    assert total == 78
    assert books == []


async def test_fetch_library_preview_lets_a_login_failure_propagate():
    with patch.object(chirp, "login", new=AsyncMock(side_effect=chirp.ChirpAuthError("nope"))):
        with pytest.raises(chirp.ChirpAuthError):
            await chirp.fetch_library_preview("a@b.com", "wrong")


# ---------------------------------------------------------- check_credentials

async def test_check_credentials_reports_success_with_the_book_count():
    with patch.object(chirp, "login", new=AsyncMock()):
        with patch.object(chirp, "fetch_library_page", new=AsyncMock(return_value=([], 78))):
            result = await chirp.check_credentials("a@b.com", "hunter2")
    assert result.ok is True
    assert "78" in result.message


async def test_check_credentials_reports_a_login_failure():
    with patch.object(chirp, "login", new=AsyncMock(side_effect=chirp.ChirpAuthError("bad password"))):
        result = await chirp.check_credentials("a@b.com", "wrong")
    assert result.ok is False
    assert "bad password" in result.message


async def test_check_credentials_reports_a_library_query_failure_separately_from_a_login_failure():
    with patch.object(chirp, "login", new=AsyncMock()):
        with patch.object(chirp, "fetch_library_page", new=AsyncMock(side_effect=chirp.ChirpRequestError("bad query"))):
            result = await chirp.check_credentials("a@b.com", "hunter2")
    assert result.ok is False
    assert "Logged in" in result.message
    assert "bad query" in result.message


async def test_check_credentials_reports_a_network_failure():
    with patch.object(chirp, "login", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        result = await chirp.check_credentials("a@b.com", "hunter2")
    assert result.ok is False
    assert "Could not reach Chirp" in result.message


# ------------------------------------------------------ cookie-session fallback

def test_client_from_cookie_header_carries_the_cookie_through():
    client = chirp.client_from_cookie_header("cf_clearance=abc; _mockingjay_session=xyz")
    assert client.headers["cookie"] == "cf_clearance=abc; _mockingjay_session=xyz"
    assert client.follow_redirects is True


def _cookie_client_stub(handler):
    """Returns a fake client_from_cookie_header that ignores the real
    network entirely, routing through the given MockTransport handler
    instead — the handler itself asserts on request.headers["cookie"] when
    the test cares what was actually carried through.
    """

    def build(cookie_header: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers={"Cookie": cookie_header}, follow_redirects=True
        )

    return build


async def test_verify_cookie_session_reports_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cookie"] == "cf_clearance=abc"
        return httpx.Response(200, json={"data": _library_payload()})

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        result = await chirp.verify_cookie_session("cf_clearance=abc")

    assert result.ok is True
    assert "78 audiobook" in result.message


async def test_verify_cookie_session_rejects_a_blank_cookie():
    result = await chirp.verify_cookie_session("   ")
    assert result.ok is False
    assert "Paste" in result.message


async def test_verify_cookie_session_reports_a_stale_session():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Just a moment...</body></html>")

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        result = await chirp.verify_cookie_session("cf_clearance=expired")

    assert result.ok is False
    assert "Cloudflare" in result.message


async def test_fetch_library_preview_via_cookie_returns_the_library():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cookie"] == "cf_clearance=abc"
        return httpx.Response(200, json={"data": _library_payload()})

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        books, total = await chirp.fetch_library_preview_via_cookie("cf_clearance=abc")

    assert total == 78
    assert len(books) == 3
