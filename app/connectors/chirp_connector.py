"""Chirp Books (chirpbooks.com) — a one-time-purchase audiobook store
("yours to keep forever", no subscription), the same purchased-library shape
as Humble/Audible/Amazon Music. No official API and no OAuth: this is a
plain Rails/Devise consumer web app, reverse-engineered the same way Amazon
Music's private API was.

Confirmed via a real capture against a live, purchased account (2026-09-29)
unless a docstring below says otherwise:
- Login is a plain form POST to /users/sign_in (authenticity_token +
  user[email]/user[password]/user[remember_me]) — no official API, no OAuth.
- The purchased-library listing and a book's own track list are both served
  by one GraphQL endpoint, /api/graphql.
- A chapter's actual media URL comes back AES-CBC encrypted
  (webPlayerMediaUrl), not a plain signed URL the way Amazon Music's is —
  decrypted client-side with a key scraped from the (authenticated-only)
  player page's HTML and an IV derived deterministically from the account's
  own numeric user id (not secret, just obfuscation: see derive_iv).
- Chirp sits behind Cloudflare — cf_clearance/__cf_bm cookies were present
  on every captured request.

**Confirmed live against a real account (2026-09-29): Cloudflare blocks
login() outright.** The same wall this codebase already hit for Humble and
Audible's own plain-login attempts (see their connectors' docstrings) —
predicted here before ever being tested, and the prediction held. login()/
check_credentials()/fetch_library_preview() are kept as-is (tested, and not
impossible some other Chirp endpoint or a future Cloudflare config change
makes them viable again), but nothing in this app's UI calls them anymore.

The actual working path, the same shape Humble's own cookie-paste fallback
already uses in this app: the user completes login in their own real
browser (which clears Cloudflare's challenge the normal way — its
resulting cf_clearance cookie is the whole point), then pastes that
browser's *entire* Cookie header back here. See client_from_cookie_header()
/ verify_cookie_session() / fetch_library_preview_via_cookie(). The full
header is asked for, not one named cookie the way Humble's single
_simpleauth_sess is — Cloudflare's clearance cookie has to ride along with
the Rails session cookie, and unlike Humble's own session cookie (this
app's own settings copy notes it "doesn't expire on a fixed schedule"),
Cloudflare's own cookies are short-lived (commonly on the order of 30
minutes to a few hours), so this will need re-pasting far more often.

NOT yet confirmed at all — reconstructed by pattern-matching a real
*response* whose matching *request* was never captured:
- The exact GraphQL operation name and pagination arguments behind the
  library listing (_LIBRARY_QUERY below). The field names are trustworthy
  (a GraphQL response can only contain fields the query actually asked
  for), but the operation name and the page/perPage argument names are an
  educated guess, not a captured fact.
- Where the decryption key (data-dk) appears for a book this account
  hasn't already opened in the web player, and whether it's per-book or
  reusable across the whole account.
- Whether purchase date and price-paid exist anywhere in Chirp's API. Not
  present in the confirmed library-listing shape; possibly on an order
  history page never yet captured.
"""

import base64
import logging
import re
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.connectors.types import CredentialStatus

logger = logging.getLogger(__name__)

BASE_URL = "https://www.chirpbooks.com"
SIGN_IN_URL = f"{BASE_URL}/users/sign_in"
LIBRARY_URL = f"{BASE_URL}/library"
GRAPHQL_URL = f"{BASE_URL}/api/graphql"

# A real browser UA — Chirp's Cloudflare protection is exactly the kind of
# thing more likely to look twice at an obviously-bare httpx default one.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/156.0"

# Common markers across Cloudflare's various challenge/interstitial pages
# (the JS "Just a moment..." challenge, the older "Checking your browser"
# page, and the "Attention Required" block page) — not exhaustive, but
# enough to tell "Cloudflare intercepted this before it reached Chirp at
# all" apart from "Chirp's own page just doesn't have a token where expected
# anymore", which need very different fixes.
_CLOUDFLARE_CHALLENGE_MARKERS = (
    "Just a moment...",
    "cdn-cgi/challenge-platform",
    "cf-browser-verification",
    "Checking your browser before accessing",
    "Attention Required! | Cloudflare",
)


def _looks_like_cloudflare_challenge(html: str) -> bool:
    return any(marker in html for marker in _CLOUDFLARE_CHALLENGE_MARKERS)


class ChirpAuthError(Exception):
    """Login failed, or a stored session is no longer valid."""


class ChirpRequestError(Exception):
    """Chirp's GraphQL API answered, but with an error payload or a shape
    this connector doesn't recognise."""


# ------------------------------------------------------------- page scraping

_CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
_CSRF_FORM_RE = re.compile(r'name="authenticity_token"\s+value="([^"]+)"')
_USER_ID_RE = re.compile(r'"userId":(\d+)')
_DECRYPTION_KEY_RE = re.compile(r'data-dk="([^"]+)"')
_AUDIOBOOK_ID_RE = re.compile(r'data-audiobook-id="([^"]+)"')


def parse_csrf_token(html: str) -> str | None:
    """Rails embeds this two ways depending on the page — confirmed via a
    real capture of /library's own <meta name="csrf-token"> tag (used by
    the site's own JS for authenticated fetch()/GraphQL calls) and via a
    real captured login POST's own body (a hidden authenticity_token form
    field) — the GET page that POST's own token came from was never itself
    captured, so the form-field regex below is Rails' well-known standard
    convention, not independently confirmed against a captured GET page.
    """
    match = _CSRF_META_RE.search(html)
    if match:
        return match.group(1)
    match = _CSRF_FORM_RE.search(html)
    return match.group(1) if match else None


def parse_user_id(html: str) -> int | None:
    """Scraped from the user-store-data div's embedded JSON — confirmed
    present on a real captured /library page from this account."""
    match = _USER_ID_RE.search(html)
    return int(match.group(1)) if match else None


def parse_decryption_key(html: str) -> str | None:
    """Scraped from a book's own player page (div.user-audiobook[data-dk])
    — the field *name* is confirmed from a working, independently-published
    implementation (jo1gi/audiobook-dl's chirp.py), not yet independently
    verified against a real captured player page from this account."""
    match = _DECRYPTION_KEY_RE.search(html)
    return match.group(1) if match else None


def parse_audiobook_id_from_player_page(html: str) -> str | None:
    match = _AUDIOBOOK_ID_RE.search(html)
    return match.group(1) if match else None


# --------------------------------------------------------------------- auth


async def login(client: httpx.AsyncClient, email: str, password: str) -> None:
    """Logs into chirpbooks.com, leaving the session cookie on `client` for
    subsequent calls. Request shape confirmed from a real captured login
    POST against this account — confirmed live (see the module docstring)
    to be blocked by Cloudflare in practice; kept working and tested rather
    than deleted, but nothing in this app's own UI calls it anymore. Use
    client_from_cookie_header()'s fallback instead for anything real.
    """
    # follow_redirects explicit here even if the client itself is already
    # configured with it — a real, independently-plausible cause of "no CSRF
    # token found" that has nothing to do with Cloudflare: without this, a
    # GET that gets redirected (a canonical-URL redirect, a locale prefix,
    # anything) comes back as the bare 3xx itself, with little or no body to
    # find a token in at all.
    get_resp = await client.get(SIGN_IN_URL, follow_redirects=True)
    token = parse_csrf_token(get_resp.text)
    if not token:
        if _looks_like_cloudflare_challenge(get_resp.text):
            logger.warning(
                "chirp: GET %s (-> %s) returned a Cloudflare challenge page instead of Chirp's own sign-in "
                "page (status %d)",
                SIGN_IN_URL,
                get_resp.url,
                get_resp.status_code,
            )
            raise ChirpAuthError(
                "Chirp's Cloudflare protection intercepted this before it ever reached the real sign-in "
                "page — a plain server-side login cannot get past that. See chirp_connector.py's own "
                "module docstring for the fallback this would need instead (reusing a browser-obtained "
                "session rather than logging in here)."
            )
        # Not a recognised Cloudflare page either — logged so the actual
        # response (page structure change? an entirely different block
        # page? a maintenance page?) is visible in Settings' own log viewer
        # instead of this being a dead end.
        logger.warning(
            "chirp: no CSRF token found on GET %s (-> %s, status %d, %d bytes). Body starts: %r",
            SIGN_IN_URL,
            get_resp.url,
            get_resp.status_code,
            len(get_resp.text),
            get_resp.text[:500],
        )
        raise ChirpAuthError("Could not find a CSRF token on Chirp's sign-in page — it may have changed.")

    resp = await client.post(
        SIGN_IN_URL,
        data={
            "authenticity_token": token,
            "user[email]": email,
            "user[password]": password,
            "user[remember_me]": "1",
            "button": "",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=True,
    )
    # Devise's own default flash copy for a rejected login — a reasonable,
    # well-known default, not yet confirmed against a real failed attempt
    # against this account (only a real successful login has been captured).
    if "/users/sign_in" in str(resp.url) or "Invalid Email or password" in resp.text:
        raise ChirpAuthError("Chirp rejected that email/password.")


# ------------------------------------------------------------------ GraphQL


async def _graphql(client: httpx.AsyncClient, operation_name: str, query: str, variables: dict) -> dict:
    resp = await client.post(
        GRAPHQL_URL,
        json={"operationName": operation_name, "query": query, "variables": variables},
        headers={"Content-Type": "application/json"},
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Logged (visible in Settings' own log viewer) with the actual body
        # rather than just the status code — an unexpected 4xx/5xx here
        # could be Chirp's own backend genuinely erroring, or a malformed
        # Cookie paste (a stray "Cookie: " prefix left in, a truncated
        # value) confusing it in a way that isn't the clean "unauthorized"
        # GraphQL error a merely-wrong-domain cookie produces.
        logger.warning(
            "chirp(%s): GraphQL call returned %d. Body starts: %r",
            operation_name,
            resp.status_code,
            resp.text[:500],
        )
        raise ChirpRequestError(f"Chirp answered with an unexpected error (status {resp.status_code}).") from exc
    try:
        payload = resp.json()
    except ValueError as exc:
        # A stale/expired cookie session (see client_from_cookie_header) is
        # expected to fail exactly this way — Cloudflare intercepts the
        # request and answers with its own HTML challenge page instead of
        # ever reaching Chirp's real API, so resp.json() has nothing valid
        # to parse. Distinguished from an unrecognised non-JSON response so
        # the message actually points at what to do about it.
        if _looks_like_cloudflare_challenge(resp.text):
            raise ChirpRequestError(
                "Cloudflare intercepted this request instead of Chirp's API answering it — "
                "the pasted cookie session is likely stale; paste a fresh one."
            ) from exc
        raise ChirpRequestError(f"Chirp did not return JSON (got: {resp.text[:300]!r})") from exc
    if payload.get("errors"):
        raise ChirpRequestError(str(payload["errors"]))
    return payload.get("data") or {}


@dataclass
class ChirpAudiobook:
    purchase_id: str
    audiobook_id: str
    title: str
    authors: str
    narrators: str
    url_path: str
    cover_url: str
    progress_status: str
    position_percent: int
    playable: bool
    series_name: str | None = None
    series_number: str | None = None


# operationName and the field selection are confirmed against a real
# captured response (see tests/fixtures/chirp_library_response.json). The
# pagination argument was NOT captured and had to be reconstructed — and
# the first live attempt (2026-09-29) confirmed half of that reconstruction
# wrong: Chirp's own schema rejected perPage outright ("Field
# 'currentUserAudiobooks' doesn't accept argument 'perPage'"), meaning the
# page size isn't caller-configurable — it's whatever Chirp's server picks
# (20, in every capture so far). page on its own has NOT yet been confirmed
# to actually page through results rather than being silently ignored too;
# that still needs a real second-page capture to know for sure.
_LIBRARY_QUERY = """
query fetchCurrentUserAudiobooks($page: Int) {
  currentUserAudiobooks(page: $page) {
    id
    progressStatus
    positionPercent
    playable
    audiobook {
      id
      url
      coverUrl
      displayTitle
      displayAuthors
      displayNarrators
      seriesAudiobook { displayNumber series { name } }
    }
  }
  currentUserAudiobooksCount
}
"""


def parse_library_page(payload: dict) -> tuple[list[ChirpAudiobook], int]:
    """payload is the "data" object of a currentUserAudiobooks response.
    Field shape confirmed from a real capture against this account (see
    tests/fixtures/chirp_library_response.json) — the pagination this
    function's own caller uses to walk multiple pages is not.
    """
    items = []
    for entry in payload.get("currentUserAudiobooks") or []:
        book = entry.get("audiobook") or {}
        series_audiobook = book.get("seriesAudiobook") or {}
        series = series_audiobook.get("series") or {}
        items.append(
            ChirpAudiobook(
                purchase_id=str(entry.get("id", "")),
                audiobook_id=str(book.get("id", "")),
                title=book.get("displayTitle", ""),
                authors=book.get("displayAuthors", ""),
                narrators=book.get("displayNarrators", ""),
                url_path=book.get("url", ""),
                cover_url=book.get("coverUrl", ""),
                progress_status=entry.get("progressStatus", ""),
                position_percent=entry.get("positionPercent") or 0,
                playable=bool(entry.get("playable")),
                series_name=series.get("name"),
                series_number=series_audiobook.get("displayNumber"),
            )
        )
    total = payload.get("currentUserAudiobooksCount", len(items))
    return items, total


async def fetch_library_page(client: httpx.AsyncClient, page: int = 1) -> tuple[list[ChirpAudiobook], int]:
    data = await _graphql(client, "fetchCurrentUserAudiobooks", _LIBRARY_QUERY, {"page": page})
    return parse_library_page(data)


@dataclass
class ChirpTrack:
    part_number: int
    chapter_number: int
    offset_from_book_start_ms: int
    duration_ms: int
    display_name: str


# Confirmed working via a real, independently-published implementation
# (jo1gi/audiobook-dl's chirp.py) — not yet independently re-verified
# against a live response from this account.
_TRACKS_QUERY = (
    "query fetchAudiobookTracks($id:ID!){audiobook(id:$id){tracks{"
    "partNumber chapterNumber offsetFromBookStartMs durationMs displayName}}}"
)
_TRACK_URL_QUERY = (
    "query fetchAudiobookTrackUrl($id:ID!,$partNumber:Int!,$chapterNumber:Int!){"
    "audiobook(id:$id){track(partNumber:$partNumber,chapterNumber:$chapterNumber){webPlayerMediaUrl}}}"
)


def parse_tracks(payload: dict) -> list[ChirpTrack]:
    tracks = (payload.get("audiobook") or {}).get("tracks") or []
    return [
        ChirpTrack(
            part_number=t["partNumber"],
            chapter_number=t["chapterNumber"],
            offset_from_book_start_ms=t["offsetFromBookStartMs"],
            duration_ms=t["durationMs"],
            display_name=t["displayName"],
        )
        for t in tracks
    ]


async def fetch_tracks(client: httpx.AsyncClient, audiobook_id: str) -> list[ChirpTrack]:
    data = await _graphql(client, "fetchAudiobookTracks", _TRACKS_QUERY, {"id": audiobook_id})
    return parse_tracks(data)


async def fetch_encrypted_track_url(client: httpx.AsyncClient, audiobook_id: str, part_number: int, chapter_number: int) -> str:
    """Returns the still-AES-encrypted webPlayerMediaUrl — pass it to
    decrypt_track_url() with this account's own key/iv before it's a
    usable download URL."""
    data = await _graphql(
        client,
        "fetchAudiobookTrackUrl",
        _TRACK_URL_QUERY,
        {"id": audiobook_id, "partNumber": part_number, "chapterNumber": chapter_number},
    )
    track = (data.get("audiobook") or {}).get("track") or {}
    return track.get("webPlayerMediaUrl", "")


# --------------------------------------------------------------- decryption


def derive_iv(user_id: int) -> bytes:
    """The AES IV Chirp's own web player derives from the account's numeric
    user id — not a secret, just an obfuscation layer: left-pad the id with
    literal "x" characters to 12 characters, then base64-encode (which
    happens to produce exactly 16 bytes — AES's block size — for any id up
    to 12 digits; presumably why 12 was chosen). Confirmed algorithm from a
    working, independently-published implementation; not yet independently
    re-derived against a real decrypted track from this account.
    """
    padded = str(user_id).rjust(12, "x")
    return base64.b64encode(padded.encode("utf-8"))


def decrypt_track_url(ciphertext_b64: str, key: bytes, iv: bytes) -> str:
    """Reverses the AES-CBC encryption Chirp's web player applies to each
    chapter's media URL before it ever reaches the client as JSON.
    Confirmed algorithm from a working, independently-published
    implementation; not yet independently verified against a real
    decrypted URL from this account.
    """
    ciphertext = base64.b64decode(ciphertext_b64)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    # Strips exactly one trailing byte rather than doing full PKCS7
    # unpadding — replicated as-is from the confirmed-working reference
    # rather than "corrected" against an assumed padding scheme.
    return plaintext.decode("utf-8")[:-1]


# ---------------------------------------------------------- connectivity check


async def fetch_library_preview(email: str, password: str, page: int = 1) -> tuple[list[ChirpAudiobook], int]:
    """Logs in fresh and fetches one page of the library — the shared
    plumbing behind both check_credentials() below (Settings' pass/fail
    check) and the Chirp page's own "Check library" button (which wants the
    actual book list, not just a status message). Raises ChirpAuthError/
    ChirpRequestError/httpx.HTTPError un-caught; callers decide how to
    present each to their own UI.
    """
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, follow_redirects=True) as client:
        await login(client, email, password)
        return await fetch_library_page(client, page=page)


async def check_credentials(email: str, password: str) -> CredentialStatus:
    """Logs in and fetches page 1 of the library as a connectivity check —
    same shape as steam_connector.check_credentials()/gog_connector's own
    equivalents. Used by Settings' own "Save" action; this is the exact
    same check scripts/verify_chirp_login.py performs standalone, just
    reachable from inside the running app instead of a one-off manual
    script.
    """
    try:
        _books, total = await fetch_library_preview(email, password)
    except ChirpAuthError as exc:
        return CredentialStatus(ok=False, message=str(exc))
    except ChirpRequestError as exc:
        return CredentialStatus(ok=False, message=f"Logged in, but the library query failed: {exc}")
    except httpx.HTTPError as exc:
        return CredentialStatus(ok=False, message=f"Could not reach Chirp: {exc}")
    return CredentialStatus(ok=True, message=f"Connected — {total} audiobook(s) found in your library.")


# ------------------------------------------------------ cookie-session fallback


def client_from_cookie_header(cookie_header: str) -> httpx.AsyncClient:
    """Builds a client carrying a browser-obtained Cookie header directly —
    the actual working path (see the module docstring): a plain server-side
    login is confirmed blocked by Cloudflare, so this instead reuses a
    session a real browser already cleared Cloudflare's own challenge with.

    Takes the *entire* Cookie header value, not one named cookie the way
    humble_connector.py's own session-key fallback does — Cloudflare's own
    clearance cookie has to ride along with the Rails session cookie for
    this to work, and which cookies Cloudflare actually checks isn't
    something to hardcode a guess about; forwarding whatever the browser
    itself sent is the faithful way to reuse that session.
    """
    return httpx.AsyncClient(headers={"User-Agent": _USER_AGENT, "Cookie": cookie_header}, follow_redirects=True)


def describe_cookie_unicode_error(cookie_header: str, exc: UnicodeEncodeError) -> str:
    """httpx encodes a plain-dict header value as strict ASCII at client
    construction time (httpx._models._normalize_header_value) — confirmed
    by reading httpx 0.28.1's own source, not guessed. A pasted cookie with
    any non-ASCII character (a smart quote/dash, a non-breaking space, or
    another invisible character some clipboard tool substituted in) raises
    UnicodeEncodeError right here, before any network call is made. Pointing
    at the exact offending character is more actionable than a generic
    "Chirp may have changed something" message, since this has nothing to
    do with Chirp itself.

    The single-ellipsis case gets its own message: confirmed live (2026-09-30)
    that a browser's *parsed* Headers view truncates a long Cookie value and
    renders a literal "…" where it cut it off, and copying from that view
    pastes the truncated text — not a stray character from a clipboard tool,
    but a whole chunk of the cookie missing. Settings' own instructions now
    lead with switching to "raw" headers to avoid this, but this message
    still needs to name it for anyone who hits it anyway.
    """
    bad_chars = cookie_header[exc.start : exc.end]
    if bad_chars == "…":
        return (
            "Your pasted Cookie value has an ellipsis ('…') at position "
            f"{exc.start} instead of the rest of the cookie — this is what a browser's "
            "*parsed* Headers view shows when it truncates a long value for display. "
            "Switch that panel to \"raw\" (or \"view source\") first, then copy the whole "
            "line from there — see the updated steps above."
        )
    return (
        f"Your pasted Cookie value contains a character HTTP headers can't carry: "
        f"{bad_chars!r} at position {exc.start}. This usually happens when copying goes "
        f"through something that substitutes smart quotes/dashes or leaves an invisible "
        f"character behind — copy the raw header value again directly from DevTools' "
        f"Network tab and paste it fresh."
    )


async def verify_cookie_session(cookie_header: str) -> CredentialStatus:
    """Same shape as check_credentials() above, for the cookie-paste
    fallback — fetches page 1 of the library directly with no login() call
    at all, since the whole point of this path is that the pasted cookie
    should already carry an authenticated, Cloudflare-cleared session.
    """
    if not cookie_header.strip():
        return CredentialStatus(ok=False, message="Paste your browser's Cookie header value first.")
    try:
        async with client_from_cookie_header(cookie_header) as client:
            _books, total = await fetch_library_page(client, page=1)
    except UnicodeEncodeError as exc:
        return CredentialStatus(ok=False, message=describe_cookie_unicode_error(cookie_header, exc))
    except ChirpRequestError as exc:
        return CredentialStatus(ok=False, message=str(exc))
    except httpx.HTTPError as exc:
        return CredentialStatus(ok=False, message=f"Could not reach Chirp: {exc}")
    return CredentialStatus(ok=True, message=f"Connected — {total} audiobook(s) found in your library.")


async def fetch_library_preview_via_cookie(cookie_header: str, page: int = 1) -> tuple[list[ChirpAudiobook], int]:
    """Cookie-session equivalent of fetch_library_preview() — used by the
    Chirp page's own "Check library" button. Raises ChirpRequestError/
    httpx.HTTPError un-caught, same contract as fetch_library_preview().
    """
    async with client_from_cookie_header(cookie_header) as client:
        return await fetch_library_page(client, page=page)
