"""GOG — like steam_connector.py, a comparison target rather than a
BaseConnector-shaped bundle source. Unlike Steam, there is no public,
self-service API key: GOG's real API (undocumented by GOG, reverse-engineered
by the community at https://gogapidocs.readthedocs.io/) uses a full OAuth2
authorization-code flow, normally driven by an embedded browser inside the
GOG Galaxy desktop client.

**A server-side callback does NOT work here — confirmed the hard way.** An
initial live test (2026-09-07) found that auth.gog.com/auth happily served a
real login page for a custom redirect_uri (this app's own /gog/callback), which
looked like proof a normal OAuth redirect flow would work. It doesn't: GOG
validates the redirect_uri against a registered allowlist later in the flow
(after a real login, not at that first page load), and a self-hosted app's own
URL was never going to be on it — confirmed by an actual attempt returning
`redirect_uri_mismatch`. The only redirect_uri this shared client_id actually
accepts is its own, `REDIRECT_URI` below (the one GOG Galaxy itself uses,
which normally an embedded browser intercepts before it ever loads). For a
web app that means: user opens the login URL, logs in on GOG's real site,
lands on a mostly-blank GOG page whose URL contains `code=...`, copies that
URL back into this app's Settings form. Same "paste a value from your
browser" shape as Humble's cookie, just a single-use/short-lived code instead
of a long-lived session cookie — meaningfully more fragile, but there is no
better option against this API without a registered redirect_uri of our own,
which nothing here has any way to obtain. client_id/client_secret below are
GOG's own (the ones GOG Galaxy itself uses), extracted by the
reverse-engineering community — not something either GOG or a user issues
per-app.

Confirmed real request/response shapes (the 200/302/400 auth-stage ones from
the docs, everything else from a real captured response on 2026-10-10 — see
_gog_image_url's own docstring for the image-field fix that capture led to):
- Token endpoint is a GET with query-string params (not a POST body) — an
  unusual, non-standard-OAuth choice, but that's what the docs show.
- GET /account/getFilteredProducts returns {"products": [...], "page":,
  "totalPages":, ...} with each product carrying isGame/isMovie booleans and
  a protocol-relative "image" URL needing both "https:" and a size suffix to
  resolve (see _gog_image_url). Every product is kept (not just isGame ones)
  — _content_type() below labels each "game"/"movie"/"other" rather than
  discarding non-games.

No purchase price or date anywhere in this connector, and that's as far as
this goes — GOG's (undocumented, reverse-engineered) API does have a
GET /account/settings/orders/data "History" endpoint under this same host
and auth, confirmed live to return {"orders": [...], "totalPages": N}, but
this account's own orders list came back empty. Unsurprising for a library
built mostly from Humble-redeemed keys rather than direct GOG purchases —
there may be nothing in "orders" to parse even for an account where this
*does* return real rows. Not pursued further without a real non-empty
example to parse from.
"""

import html as html_module
import logging
import re
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

AUTH_URL = "https://auth.gog.com/auth"
TOKEN_URL = "https://auth.gog.com/token"
API_BASE = "https://embed.gog.com"
WEB_BASE = "https://www.gog.com"
CLIENT_ID = "46899977096215655"
CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
# The only redirect_uri this client_id actually accepts (confirmed via a real
# redirect_uri_mismatch on any other value) — GOG's own page, normally
# intercepted by an embedded browser inside the Galaxy client before it loads.
REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"

# AUTH_URL/LOGIN_URL are never fetched by this app's own httpx client — they're
# a link shown to the user for a real external browser login, which can't be
# mocked (see routers/settings.py's demo-mode shortcut for how demo mode
# handles GOG instead). Only the two direct API calls below get redirected.
LOGIN_URL = f"{AUTH_URL}?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&layout=client2"

_CODE_RE = re.compile(r"[?&]code=([^&\s]+)")


def _token_url() -> str:
    return f"{settings.mock_api_base_url}/gog/token" if settings.demo_mode else TOKEN_URL


def _api_base() -> str:
    return f"{settings.mock_api_base_url}/gog" if settings.demo_mode else API_BASE


def _web_base() -> str:
    return f"{settings.mock_api_base_url}/gog" if settings.demo_mode else WEB_BASE


CONTENT_TYPE_GAME = "game"
CONTENT_TYPE_MOVIE = "movie"
CONTENT_TYPE_OTHER = "other"


class GogGameData:
    def __init__(self, product_id: int, title: str, image_url: str, content_type: str = CONTENT_TYPE_GAME):
        self.product_id = product_id
        self.title = title
        self.image_url = image_url
        self.content_type = content_type


def _content_type(product: dict) -> str:
    if product.get("isGame"):
        return CONTENT_TYPE_GAME
    if product.get("isMovie"):
        return CONTENT_TYPE_MOVIE
    return CONTENT_TYPE_OTHER


class GogAuthError(Exception):
    """Raised when the code exchange or a token refresh fails."""


def extract_code(pasted: str) -> str:
    """Accepts either the full redirected URL the user copied out of their
    browser's address bar, or just the bare code value itself."""
    pasted = pasted.strip()
    match = _CODE_RE.search(pasted)
    return match.group(1) if match else pasted


def _describe_error(resp: httpx.Response) -> str:
    """GOG's OAuth errors normally follow the standard {"error": ...,
    "error_description": ...} shape — surface the real reason (invalid_grant,
    redirect_uri_mismatch, etc.) instead of just the bare HTTP status, since a
    generic "may have expired" guess isn't distinguishable from a real bug on
    our end without it.
    """
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict) and (body.get("error") or body.get("error_description")):
        parts = [str(body.get(k)) for k in ("error", "error_description") if body.get(k)]
        return f"HTTP {resp.status_code}: {' — '.join(parts)}"
    return f"HTTP {resp.status_code}"


async def exchange_code(code: str) -> dict:
    """Returns {"access_token": ..., "refresh_token": ...}. Raises GogAuthError
    on any failure — the code is single-use and short-lived (confirmed real
    HTTP 400 from a garbage/expired code), so this normally only fails from a
    slow user or a reused/stale one."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            _token_url(),
            params={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )
    if resp.status_code != 200:
        raise GogAuthError(f"GOG rejected the code ({_describe_error(resp)}). Log in again and paste a fresh one right away.")
    data = resp.json()
    if "access_token" not in data or "refresh_token" not in data:
        raise GogAuthError("GOG's response didn't include the expected tokens.")
    return data


async def refresh_access_token(refresh_token: str) -> dict:
    """Returns a fresh {"access_token": ..., "refresh_token": ...} — GOG may
    rotate the refresh token on each use, so callers must persist whatever
    comes back, not just reuse the original indefinitely."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            _token_url(),
            params={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    if resp.status_code != 200:
        raise GogAuthError(f"GOG rejected the stored refresh token ({_describe_error(resp)}) — reconnect in Settings.")
    data = resp.json()
    if "access_token" not in data:
        raise GogAuthError("GOG's refresh response didn't include an access token.")
    return data


def _gog_image_url(image: str) -> str:
    """Confirmed live (2026-10-10) via a real captured product: "image" is a
    protocol-relative path to a bare content hash with no extension at all
    (e.g. "//images-3.gog-statics.com/<hash>") — fetching that path directly
    404s. GOG's own site appends a size suffix to pick a rendition; "_196"
    (one of its own breakpoints, not an arbitrary width — "_400" 400s while
    "_392" and "_196" both 200) is confirmed to resolve to a real
    image/jpeg. A path that already carries its own extension (seen nowhere
    in the one real sample so far, but the docs give no guarantee every
    product looks the same) is left alone rather than double-suffixed.
    """
    if not image.startswith("//"):
        return image
    base = f"https:{image}"
    if base.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return base
    return f"{base}_196.jpg"


async def fetch_owned_games(access_token: str) -> list[GogGameData]:
    games: list[GogGameData] = []
    headers = {"Authorization": f"Bearer {access_token}"}
    page, total_pages = 1, 1
    async with httpx.AsyncClient(timeout=30) as client:
        while page <= total_pages:
            resp = await client.get(f"{_api_base()}/account/getFilteredProducts", params={"page": page}, headers=headers)
            if resp.status_code != 200:
                raise GogAuthError(f"GOG rejected the games request (HTTP {resp.status_code}).")
            data = resp.json()
            total_pages = int(data.get("totalPages") or 1)
            for p in data.get("products") or []:
                games.append(
                    GogGameData(
                        product_id=p["id"],
                        title=p.get("title") or f"Product {p['id']}",
                        image_url=_gog_image_url(p.get("image") or ""),
                        content_type=_content_type(p),
                    )
                )
            page += 1
    return games


class GogOrderItem:
    """One product line item from the account's own order-history page —
    not one row per order, one per product, since a bundle order (e.g.
    "Deus Ex Bundle") is itself a single line with its own title and price,
    never matching any individual owned GogGame.title. Those games just
    end up with no purchase data from this source — the same honest gap a
    Humble-redeemed GOG key already has, since a key activation is never a
    GOG "order" at all and never appears here either.
    """

    def __init__(self, title: str, price_amount: float | None, purchased_at: datetime | None):
        self.title = title
        self.price_amount = price_amount
        self.purchased_at = purchased_at


_ORDER_START = re.compile(r'gog-order-item="[^"]+"')
_ORDER_DATE = re.compile(r'gog-relative-time="(\d+)"')
_PRODUCT_ROW_START = re.compile(r'class="product-state-holder product-row\s+order-product"')
_PRODUCT_TITLE = re.compile(r'hook-test="productTitle">([^<]*)<')
_PRODUCT_PRICE = re.compile(r'data-cy="product-price">\s*([\d.,]+)\s*<')


def _parse_gog_price(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def parse_order_history(html: str) -> list[GogOrderItem]:
    """www.gog.com/en/account/settings/orders — behind the account's own
    website session (cookie), a completely separate auth path from the
    OAuth token the rest of this connector uses for the Galaxy library API.

    Confirmed live (2026-10-10) via real captured rendered markup for a
    Free order; a paid order's fields were cross-checked only against a
    real screenshot (same field names/positions, real numbers in place of
    "0.00") — never independently captured as raw HTML, so treat anything
    beyond title/price/date as unconfirmed.

    Every order loads in a single fetch — confirmed live that neither the
    URL nor any network request changes when moving between the page's own
    "pages"; that pagination is a purely client-side display window over
    data already fully present, not real server-side pagination.
    """
    items: list[GogOrderItem] = []
    order_starts = list(_ORDER_START.finditer(html))
    for index, match in enumerate(order_starts):
        chunk_end = order_starts[index + 1].start() if index + 1 < len(order_starts) else len(html)
        chunk = html[match.end():chunk_end]

        date_match = _ORDER_DATE.search(chunk)
        purchased_at = datetime.utcfromtimestamp(int(date_match.group(1))) if date_match else None

        product_starts = list(_PRODUCT_ROW_START.finditer(chunk))
        for p_index, p_match in enumerate(product_starts):
            p_end = product_starts[p_index + 1].start() if p_index + 1 < len(product_starts) else len(chunk)
            p_chunk = chunk[p_match.end():p_end]

            title_match = _PRODUCT_TITLE.search(p_chunk)
            if not title_match:
                continue
            price_match = _PRODUCT_PRICE.search(p_chunk)

            items.append(
                GogOrderItem(
                    title=html_module.unescape(title_match.group(1).strip()),
                    price_amount=_parse_gog_price(price_match.group(1) if price_match else None),
                    purchased_at=purchased_at,
                )
            )
    return items


async def fetch_order_history(cookie: str) -> list[GogOrderItem]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(f"{_web_base()}/en/account/settings/orders", headers={"Cookie": cookie})
    if resp.status_code != 200:
        raise GogAuthError(f"GOG rejected the order history request (HTTP {resp.status_code}).")
    items = parse_order_history(resp.text)
    if not items:
        # A 200 with zero parsed orders is ambiguous on its own, and the
        # obvious causes (no orders at all, a logged-out response that still
        # 200s, client-side-JS-only rendering) were already ruled out live —
        # gog-order-item= wrappers ARE present in the raw response. What's
        # left is a structural mismatch somewhere inside them: these counts
        # pinpoint which stage breaks (order wrapper vs. product-row div vs.
        # title span) without needing another full-page capture, and the
        # snippet shows what the first real order's own markup actually
        # looks like — the one real order parse_order_history was built
        # from was a direct, Google-Pay-paid GOG order; most real accounts'
        # orders are distributor-sourced (bundle-redeemed keys activated on
        # GOG), which the template's own ng-show="::(!order.distributor...)"
        # guards suggest may render differently inside.
        order_matches = list(_ORDER_START.finditer(resp.text))
        first_order_snippet = ""
        if order_matches:
            start = order_matches[0].end()
            first_order_snippet = resp.text[start : start + 1500]
        logger.warning(
            "gog(orders): parsed 0 orders from a 200 response (%d bytes). "
            "order blocks: %d, product-row divs: %d, product titles: %d. "
            "First order's own markup starts: %r",
            len(resp.text),
            len(order_matches),
            len(_PRODUCT_ROW_START.findall(resp.text)),
            len(_PRODUCT_TITLE.findall(resp.text)),
            first_order_snippet,
        )
    return items
