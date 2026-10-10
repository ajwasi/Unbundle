"""A real, headless browser for the handful of pages a plain httpx request
can never see correctly — either because the page is rendered client-side
(GOG's order-history page, confirmed 2026-10-10: the server's own raw
response is Angular's uncompiled template, {{ }} placeholders and all, never
real data) or because a request-level gate rejects a bare HTTP client
outright regardless of cookie validity (Chirp's player page, confirmed
2026-10-06: a Cloudflare challenge even with a cookie the same session
already used successfully elsewhere).

Reuses the exact credential every other connector already stores — a raw
Cookie header value pasted from the user's own browser — rather than
needing a separate login flow of its own. A fresh headless context gets
that same cookie injected before navigating, so from the target site's
point of view this looks like the session the user's own browser already
established, not a new one.

A browser is launched fresh per call and torn down immediately after,
never kept running between calls — this is used only for occasional,
user-triggered actions (a refresh, a download), not on every page view, so
the cost of a fresh launch (a second or two) is a non-issue next to the
cost of holding a Chromium process resident in a self-hosted container
between uses.

--no-sandbox is passed because Chromium's own OS-level sandbox commonly
cannot set up its usual namespaces inside a Docker container without extra
host capabilities this app has no way to require a deployer grant — the
standard, documented tradeoff for headless Chromium in Docker.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Page, async_playwright


def parse_cookie_header(cookie_header: str, domain: str) -> list[dict]:
    """"name=value; name2=value2" (the same flat string every connector's
    own httpx calls already send as a Cookie header) -> Playwright's own
    per-cookie dict shape, which needs a domain/path Playwright can match
    requests against — a raw header string carries neither. domain should
    carry a leading "." to cover the site's subdomains, matching how a
    first-party cookie set by a real browser normally scopes itself.
    """
    cookies = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
    return cookies


@asynccontextmanager
async def browser_page(cookie_header: str, domain: str) -> AsyncIterator[Page]:
    """One headless Chromium page, with cookie_header's cookies already
    applied for domain, torn down automatically on exit (page, context and
    browser all close even if the caller raises)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        try:
            context = await browser.new_context()
            cookies = parse_cookie_header(cookie_header, domain)
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            try:
                yield page
            finally:
                await page.close()
        finally:
            await browser.close()


async def fetch_rendered_html(url: str, cookie_header: str, domain: str, timeout_ms: int = 30000) -> str:
    """Navigates to url with cookie_header's cookies already applied, waits
    for the page's own network activity to settle (so client-side-rendered
    content has actually had a chance to populate), and returns the real
    rendered HTML — unlike a plain httpx GET, this sees whatever the page's
    own JavaScript produced, not just the server's initial response.
    """
    async with browser_page(cookie_header, domain) as page:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        return await page.content()
