from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.browser import browser_page, fetch_rendered_html, parse_cookie_header


def test_parse_cookie_header_splits_and_trims_pairs():
    cookies = parse_cookie_header("a=1; b = 2 ;c=3", ".example.com")
    assert cookies == [
        {"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
        {"name": "b", "value": "2", "domain": ".example.com", "path": "/"},
        {"name": "c", "value": "3", "domain": ".example.com", "path": "/"},
    ]


def test_parse_cookie_header_skips_malformed_segments():
    assert parse_cookie_header("a=1; ; novalue; =emptyname", ".example.com") == [
        {"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
    ]


def test_parse_cookie_header_handles_an_empty_string():
    assert parse_cookie_header("", ".example.com") == []


def _mock_playwright(page_content="<html>rendered</html>"):
    """A fake async_playwright() whose chromium.launch()/new_context()/
    new_page() chain matches real Playwright's own shape closely enough to
    exercise browser.py's orchestration without launching a real browser —
    the real module itself was validated manually against a real headless
    Chromium instance before this test was written; this just locks in that
    the right calls happen in the right order and everything gets closed.
    """
    page = AsyncMock()
    page.content = AsyncMock(return_value=page_content)

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    chromium = AsyncMock()
    chromium.launch = AsyncMock(return_value=browser)

    pw_instance = MagicMock()
    pw_instance.chromium = chromium

    pw_cm = AsyncMock()
    pw_cm.__aenter__ = AsyncMock(return_value=pw_instance)
    pw_cm.__aexit__ = AsyncMock(return_value=False)

    return pw_cm, chromium, browser, context, page


async def test_fetch_rendered_html_injects_cookies_and_returns_content():
    pw_cm, chromium, browser, context, page = _mock_playwright("<html>real content</html>")
    with patch("app.browser.async_playwright", return_value=pw_cm):
        html = await fetch_rendered_html("https://x.example.com/page", "a=1; b=2", ".example.com")

    assert html == "<html>real content</html>"
    context.add_cookies.assert_awaited_once_with(
        [
            {"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
            {"name": "b", "value": "2", "domain": ".example.com", "path": "/"},
        ]
    )
    page.goto.assert_awaited_once()
    assert page.goto.call_args.args[0] == "https://x.example.com/page"
    assert page.goto.call_args.kwargs["wait_until"] == "networkidle"
    chromium.launch.assert_awaited_once_with(args=["--no-sandbox"])
    page.close.assert_awaited_once()
    browser.close.assert_awaited_once()


async def test_fetch_rendered_html_skips_add_cookies_when_header_is_empty():
    pw_cm, chromium, browser, context, page = _mock_playwright()
    with patch("app.browser.async_playwright", return_value=pw_cm):
        await fetch_rendered_html("https://x.example.com/page", "", ".example.com")
    context.add_cookies.assert_not_called()


async def test_browser_page_closes_everything_even_if_the_caller_raises():
    pw_cm, chromium, browser, context, page = _mock_playwright()
    with patch("app.browser.async_playwright", return_value=pw_cm):
        with pytest.raises(RuntimeError):
            async with browser_page("a=1", ".example.com") as p:
                assert p is page
                raise RuntimeError("boom")
    page.close.assert_awaited_once()
    browser.close.assert_awaited_once()
