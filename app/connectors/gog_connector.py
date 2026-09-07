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

Confirmed real request/response shapes from the docs (not GOG's own, so
treat as best-effort — no live success response has been seen from this
environment, only the real 200/302/400 auth-stage responses noted above/below):
- Token endpoint is a GET with query-string params (not a POST body) — an
  unusual, non-standard-OAuth choice, but that's what the docs show.
- GET /account/getFilteredProducts returns {"products": [...], "page":,
  "totalPages":, ...} with each product carrying isGame/isMovie booleans and
  a protocol-relative "image" URL (needs "https:" prefixed to be usable).
"""

import re

import httpx

AUTH_URL = "https://auth.gog.com/auth"
TOKEN_URL = "https://auth.gog.com/token"
API_BASE = "https://embed.gog.com"
CLIENT_ID = "46899977096215655"
CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
# The only redirect_uri this client_id actually accepts (confirmed via a real
# redirect_uri_mismatch on any other value) — GOG's own page, normally
# intercepted by an embedded browser inside the Galaxy client before it loads.
REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"

LOGIN_URL = f"{AUTH_URL}?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&layout=client2"

_CODE_RE = re.compile(r"[?&]code=([^&\s]+)")


class GogGameData:
    def __init__(self, product_id: int, title: str, image_url: str):
        self.product_id = product_id
        self.title = title
        self.image_url = image_url


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
            TOKEN_URL,
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
            TOKEN_URL,
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


async def fetch_owned_games(access_token: str) -> list[GogGameData]:
    games: list[GogGameData] = []
    headers = {"Authorization": f"Bearer {access_token}"}
    page, total_pages = 1, 1
    async with httpx.AsyncClient(timeout=30) as client:
        while page <= total_pages:
            resp = await client.get(f"{API_BASE}/account/getFilteredProducts", params={"page": page}, headers=headers)
            if resp.status_code != 200:
                raise GogAuthError(f"GOG rejected the games request (HTTP {resp.status_code}).")
            data = resp.json()
            total_pages = int(data.get("totalPages") or 1)
            for p in data.get("products") or []:
                if not p.get("isGame", True):
                    continue  # movies/other media share this endpoint — games only
                image = p.get("image") or ""
                games.append(
                    GogGameData(
                        product_id=p["id"],
                        title=p.get("title") or f"Product {p['id']}",
                        image_url=f"https:{image}" if image.startswith("//") else image,
                    )
                )
            page += 1
    return games
