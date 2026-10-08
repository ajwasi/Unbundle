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
  present in the confirmed library-listing shape; a real capture of
  /purchases (2026-09-30) confirmed that page exists and is itself
  paginated, but its response shape hasn't been captured yet, so this is
  still open. currentProduct.listingPrice/discountPrice *is* now
  confirmed and requested (see _LIBRARY_QUERY) — today's storefront price,
  not what this account paid, which is exactly the distinction the missing
  purchase-price field above is about.
- Whether the page argument above actually pages through results or is
  silently ignored by Chirp's server (a real second-page capture would
  settle it either way; still doesn't exist). chirp_sync.py's own
  page-walking loop is written defensively because of this: it stops as
  soon as a page stops introducing new purchase ids, rather than assuming
  pagination works and spinning or returning duplicates if it doesn't.
"""

import base64
import json
import html as html_module
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

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
    # Today's storefront price, confirmed via currentProduct — not what this
    # account paid (Chirp's API has no purchase-price field anywhere; see
    # this module's own docstring). None when currentProduct is null, which
    # the real capture shows happens for at least some owned books.
    listing_price: float | None = None
    discount_price: float | None = None


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
      currentProduct { listingPrice discountPrice }
    }
  }
  currentUserAudiobooksCount
}
"""


def _parse_price(value) -> float | None:
    """Chirp's own currentProduct prices are strings like "$22.95", confirmed
    from a real capture (tests/fixtures/chirp_library_response.json) — never
    seen as a bare number, so the "$" strip is load-bearing, not defensive
    padding."""
    if not value or not isinstance(value, str):
        return None
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


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
        current_product = book.get("currentProduct") or {}
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
                listing_price=_parse_price(current_product.get("listingPrice")),
                discount_price=_parse_price(current_product.get("discountPrice")),
            )
        )
    total = payload.get("currentUserAudiobooksCount", len(items))
    return items, total


async def fetch_library_page(client: httpx.AsyncClient, page: int = 1) -> tuple[list[ChirpAudiobook], int]:
    data = await _graphql(client, "fetchCurrentUserAudiobooks", _LIBRARY_QUERY, {"page": page})
    return parse_library_page(data)


@dataclass
class ChirpPurchase:
    item_id: str
    url_path: str
    title: str
    purchased_at: date | None
    paid_price: float | None
    list_price: float | None
    is_free: bool


_MAX_PURCHASE_PAGES = 100
_PURCHASE_ITEM_MARKER = 'data-purchase-item-id="'
_ORDINAL_SUFFIX = re.compile(r"(\d+)(st|nd|rd|th)")


def _parse_purchase_date(text: str) -> date | None:
    cleaned = _ORDINAL_SUFFIX.sub(r"\1", text.strip())
    try:
        return datetime.strptime(cleaned, "%B %d, %Y").date()
    except ValueError:
        return None


def parse_purchases_page(page_html: str) -> tuple[list[ChirpPurchase], str | None]:
    """One /purchases order-history page, server-rendered (confirmed from a
    real capture — no XHR involved). Returns the purchases on it, plus the
    relative URL of the next page if one exists. Each order's date applies to
    every book inside it; each book's invoice-price is what was actually paid,
    list-price is the struck-through original.
    """
    purchases: list[ChirpPurchase] = []
    for order_chunk in page_html.split('<div class="purchase">')[1:]:
        date_match = re.search(r'<div class="purchase-date">\s*(.*?)\s*</div>', order_chunk, re.S)
        purchased_at = _parse_purchase_date(date_match.group(1)) if date_match else None

        for item_chunk in order_chunk.split(_PURCHASE_ITEM_MARKER)[1:]:
            item_id_match = re.match(r'(\d+)"', item_chunk)
            url_match = re.search(r'<a href="(/audiobooks/[^"]+)"', item_chunk)
            title_match = re.search(r'<div class="title">\s*<a[^>]*>(.*?)</a>', item_chunk, re.S)
            if not (item_id_match and url_match and title_match):
                continue
            list_match = re.search(r'<div class="list-price( free)?">\s*(.*?)\s*</div>', item_chunk, re.S)
            invoice_match = re.search(r'<div class="invoice-price">\s*(.*?)\s*</div>', item_chunk, re.S)
            is_free = bool(list_match and list_match.group(1))
            purchases.append(
                ChirpPurchase(
                    item_id=item_id_match.group(1),
                    url_path=url_match.group(1),
                    title=html_module.unescape(title_match.group(1).strip()),
                    purchased_at=purchased_at,
                    paid_price=0.0 if is_free else _parse_price(invoice_match.group(1) if invoice_match else None),
                    list_price=None if is_free or not list_match else _parse_price(list_match.group(2)),
                    is_free=is_free,
                )
            )

    next_match = re.search(r'<a[^>]*\brel="next"[^>]*\bhref="([^"]+)"', page_html)
    return purchases, html_module.unescape(next_match.group(1)) if next_match else None


async def fetch_purchases(client: httpx.AsyncClient) -> list[ChirpPurchase]:
    """Walks every page of order history. Stops at the first page with no
    next link, on a repeated URL, or at _MAX_PURCHASE_PAGES. A session that's
    expired (or a Cloudflare challenge) shows up as a page with no purchase
    list at all, which is reported rather than read as an empty history.
    """
    purchases: list[ChirpPurchase] = []
    seen_item_ids: set[str] = set()
    seen_urls: set[str] = set()
    url: str | None = f"{BASE_URL}/purchases"

    while url and url not in seen_urls and len(seen_urls) < _MAX_PURCHASE_PAGES:
        seen_urls.add(url)
        resp = await client.get(url)
        if _looks_like_cloudflare_challenge(resp.text):
            raise ChirpRequestError(
                "Cloudflare intercepted the order history request — paste a fresh Cookie header value in Settings."
            )
        if resp.status_code >= 400:
            logger.warning(
                "chirp(purchases): order history returned %d. Body starts: %r",
                resp.status_code, resp.text[:300],
            )
            raise ChirpRequestError(
                f"Chirp refused the order history request (status {resp.status_code}). "
                "The pasted cookie session has most likely expired — paste a fresh Cookie header value in Settings."
            )
        if 'class="purchases-list"' not in resp.text:
            raise ChirpRequestError(
                "Chirp's order history didn't load — the pasted cookie session is likely stale; paste a fresh one."
            )

        page_purchases, next_path = parse_purchases_page(resp.text)
        for purchase in page_purchases:
            if purchase.item_id not in seen_item_ids:
                seen_item_ids.add(purchase.item_id)
                purchases.append(purchase)
        url = urljoin(BASE_URL, next_path) if next_path else None

    return purchases


@dataclass
class ChirpWishlistEntry:
    url_path: str
    title: str
    authors: str
    cover_url: str
    current_price: float | None
    list_price: float | None


_WISHLIST_ITEM_START = re.compile(r'wishlistItem[^"]*" role="listitem" aria-label="([^"]*)"')
_WISHLIST_GRID_MARKER = "bookGridWide"
_WISHLIST_CAROUSEL_MARKER = "userRelatedAudiobooksCarousel"


def parse_wishlist_page(page_html: str) -> list[ChirpWishlistEntry]:
    """The wishlist grid itself. Everything below the grid is a "Books You
    May Also Like" carousel that uses the same list-item markup, so the grid
    is cut out first rather than matching items from the whole page.
    """
    grid_start = page_html.find(_WISHLIST_GRID_MARKER)
    if grid_start == -1:
        return []
    carousel_start = page_html.find(_WISHLIST_CAROUSEL_MARKER, grid_start)
    grid = page_html[grid_start: carousel_start if carousel_start != -1 else None]

    starts = list(_WISHLIST_ITEM_START.finditer(grid))
    entries: list[ChirpWishlistEntry] = []
    for index, match in enumerate(starts):
        chunk_end = starts[index + 1].start() if index + 1 < len(starts) else len(grid)
        chunk = grid[match.end():chunk_end]

        url_match = re.search(r'href="(/audiobooks/[^"]+)"', chunk)
        if not url_match:
            continue
        title_match = re.search(r"<h3[^>]*>\s*<a[^>]*>(.*?)</a>", chunk, re.S)
        byline_match = re.search(r"<h4[^>]*>(.*?)</h4>", chunk, re.S)
        cover_match = re.search(r'<img[^>]*\bsrc="([^"]+)"', chunk)
        discount_match = re.search(r'discountPrice[^"]*"[^>]*>\s*([^<]*?)\s*</div>', chunk)
        listing_match = re.search(r'listingPrice[^"]*"[^>]*>\s*([^<]*?)\s*</div>', chunk)

        current = _parse_price(discount_match.group(1)) if discount_match else None
        listing = _parse_price(listing_match.group(1)) if listing_match else None
        if current is None:
            current = listing
            listing = None

        title = title_match.group(1).strip() if title_match else match.group(1)
        authors = re.sub(r"<[^>]+>", "", byline_match.group(1)).replace("by", "", 1).strip() if byline_match else ""
        entries.append(
            ChirpWishlistEntry(
                url_path=url_match.group(1),
                title=html_module.unescape(title),
                authors=html_module.unescape(authors),
                cover_url=cover_match.group(1) if cover_match else "",
                current_price=current,
                list_price=listing if listing is not None and current is not None and listing > current else None,
            )
        )
    return entries


async def fetch_wishlist(client: httpx.AsyncClient) -> list[ChirpWishlistEntry]:
    resp = await client.get(f"{BASE_URL}/wishlist")
    if _looks_like_cloudflare_challenge(resp.text):
        raise ChirpRequestError(
            "Cloudflare intercepted the wishlist request — paste a fresh Cookie header value in Settings."
        )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("chirp(wishlist): request returned %d. Body starts: %r", resp.status_code, resp.text[:300])
        raise ChirpRequestError(f"Chirp's wishlist answered with status {exc.response.status_code}.") from exc
    if 'id="wishlist-app"' not in resp.text:
        raise ChirpRequestError(
            "Chirp's wishlist didn't load — the pasted cookie session is likely stale; paste a fresh one."
        )
    return parse_wishlist_page(resp.text)


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


async def probe_first_track(client: httpx.AsyncClient, purchase_id: str, audiobook_id: str) -> dict:
    """Read-only: walks the playback chain for one book's first chapter and
    reports each step, without saving any media. The key, the signed media
    URL's query string and the audio bytes themselves are never returned —
    only the host, extension, status, content type and the first bytes.

    **Confirmed live (2026-10-06): Cloudflare blocks the player page itself**
    (/player/{id}), even with a cookie that the same request session already
    used successfully for the library, order history and GraphQL calls. The
    same wall this codebase already hit for Humble, Audible and Chirp's own
    login — a download feature built on this path isn't viable with a plain
    cookie reuse. Kept here as a record and in case that ever changes; the
    diagnostic route that called this (/chirp/probe-download) has been
    removed now that the question it existed to answer has one.
    """
    resp = await client.get(f"{BASE_URL}/player/{purchase_id}")
    if _looks_like_cloudflare_challenge(resp.text):
        raise ChirpRequestError(
            "Cloudflare intercepted the player page request — paste a fresh Cookie header value in Settings."
        )
    if resp.status_code >= 400:
        logger.warning(
            "chirp(probe): player page returned %d. Body starts: %r",
            resp.status_code, resp.text[:300],
        )
        raise ChirpRequestError(
            f"Chirp refused the player page request (status {resp.status_code}). The pasted cookie session may "
            "be stale, or the player page may need headers (e.g. a Referer) this probe doesn't send yet."
        )
    html = resp.text
    key = parse_decryption_key(html)
    user_id = parse_user_id(html)
    report: dict = {
        "player_page_bytes": len(html),
        "key_found": key is not None,
        "key_length": len(key) if key else 0,
        "user_id_found": user_id is not None,
    }
    if key is None or user_id is None:
        report["stopped_at"] = "player page did not contain a key and user id"
        return report

    tracks = await fetch_tracks(client, audiobook_id)
    report["track_count"] = len(tracks)
    if not tracks:
        report["stopped_at"] = "no tracks returned"
        return report
    first = tracks[0]
    report["first_track"] = {"part": first.part_number, "chapter": first.chapter_number, "duration_ms": first.duration_ms}

    ciphertext = await fetch_encrypted_track_url(client, audiobook_id, first.part_number, first.chapter_number)
    report["ciphertext_length"] = len(ciphertext)
    if not ciphertext:
        report["stopped_at"] = "no encrypted media URL returned"
        return report

    try:
        media_url = decrypt_track_url(ciphertext, key.encode("utf-8"), derive_iv(user_id))
    except ValueError as exc:
        report["stopped_at"] = f"decryption failed: {type(exc).__name__}"
        return report

    parsed = urlparse(media_url)
    report["media_host"] = parsed.netloc
    report["media_extension"] = parsed.path.rsplit(".", 1)[-1] if "." in parsed.path else ""
    head = b""
    async with client.stream("GET", media_url) as resp:
        report["media_status"] = resp.status_code
        report["content_type"] = resp.headers.get("content-type", "")
        report["content_length"] = resp.headers.get("content-length")
        async for chunk in resp.aiter_bytes():
            head += chunk
            if len(head) >= 32:
                break
    report["first_bytes_hex"] = head[:16].hex()
    report["mp4_box_type_at_offset_4"] = head[4:8].decode("ascii", "replace")
    return report
@dataclass
class ChirpSeriesBook:
    url_path: str
    title: str
    authors: str
    series_number: str
    listing_price: float | None
    current_price: float | None


_AUDIOBOOK_DATA_ATTR = re.compile(r'data-audiobook="([^"]*)"')
_SERIES_CARD_START = re.compile(r'data-qa="book-card-collection-item-\d+"')


def parse_series_url(book_html: str) -> str | None:
    """A book page's own JSON (its data-audiobook attribute) names its series
    as seriesUrl, e.g. "/series/the-silo-saga-audiobooks"."""
    match = _AUDIOBOOK_DATA_ATTR.search(book_html)
    if not match:
        return None
    try:
        data = json.loads(html_module.unescape(match.group(1)))
    except ValueError:
        return None
    series_url = data.get("seriesUrl")
    return series_url if isinstance(series_url, str) and series_url.startswith("/series/") else None


def parse_series_books(series_html: str) -> list[ChirpSeriesBook]:
    starts = list(_SERIES_CARD_START.finditer(series_html))
    books: list[ChirpSeriesBook] = []
    for index, match in enumerate(starts):
        chunk_end = starts[index + 1].start() if index + 1 < len(starts) else len(series_html)
        chunk = series_html[match.end():chunk_end]

        url_match = re.search(r'href="(/audiobooks/[^"]+)"', chunk)
        title_match = re.search(r"<h3[^>]*>\s*<a[^>]*>(.*?)</a>", chunk, re.S)
        if not (url_match and title_match):
            continue
        byline_match = re.search(r"<h4[^>]*>(.*?)</h4>", chunk, re.S)
        number_match = re.search(r'data-testid="series-number">\s*(.*?)\s*</span>', chunk, re.S)
        discount_match = re.search(r'discountPrice[^"]*"[^>]*>\s*([^<]*?)\s*</div>', chunk)
        listing_match = re.search(r'listingPrice[^"]*"[^>]*>\s*([^<]*?)\s*</div>', chunk)

        current = _parse_price(discount_match.group(1)) if discount_match else None
        listing = _parse_price(listing_match.group(1)) if listing_match else None
        if current is None:
            current = listing
            listing = None

        number = number_match.group(1).replace("Book #", "").strip() if number_match else ""
        authors = re.sub(r"<[^>]+>", "", byline_match.group(1)).replace("by", "", 1).strip() if byline_match else ""
        books.append(
            ChirpSeriesBook(
                url_path=url_match.group(1),
                title=html_module.unescape(re.sub(r"<[^>]+>", "", title_match.group(1)).strip()),
                authors=html_module.unescape(authors),
                series_number=number,
                listing_price=listing if listing is not None and current is not None and listing > current else None,
                current_price=current,
            )
        )
    return books


async def _get_page(client: httpx.AsyncClient, url: str) -> str:
    """Same "Cloudflare challenge checked before status, body snippet logged
    on failure" order as the player-page and order-history fetches — a 403
    here was previously reported as a bare status with no way to tell a
    Cloudflare block apart from anything else Chirp might answer with.
    """
    resp = await client.get(url)
    if _looks_like_cloudflare_challenge(resp.text):
        raise ChirpRequestError(f"Cloudflare intercepted the request to {url} — paste a fresh Cookie header value in Settings.")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("chirp(page): %s returned %d. Body starts: %r", url, exc.response.status_code, resp.text[:300])
        raise ChirpRequestError(f"Chirp answered {url} with status {exc.response.status_code}.") from exc
    return resp.text


async def fetch_series_url_for_book(client: httpx.AsyncClient, book_url_path: str) -> str | None:
    return parse_series_url(await _get_page(client, urljoin(BASE_URL, book_url_path)))


async def fetch_series_books(client: httpx.AsyncClient, series_url: str) -> list[ChirpSeriesBook]:
    return parse_series_books(await _get_page(client, urljoin(BASE_URL, series_url)))
