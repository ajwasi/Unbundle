from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.connectors import chirp_connector as chirp

FIXTURE = Path(__file__).parent / "fixtures" / "chirp_purchases_page.html"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_purchases_page_reads_each_book_with_its_order_date():
    items, _next = chirp.parse_purchases_page(_fixture_html())

    assert [i.title for i in items] == ["Sand", "The Dark Forest", "Wolf's Bane"]
    sand = items[0]
    assert sand.url_path == "/audiobooks/sand-by-hugh-howey-f6379e10a9"
    assert sand.purchased_at == date(2025, 12, 1)
    assert sand.paid_price == 7.99
    assert sand.list_price == 24.99
    assert sand.is_free is False


def test_a_book_in_a_later_order_gets_that_orders_date():
    items, _ = chirp.parse_purchases_page(_fixture_html())
    assert items[2].purchased_at == date(2025, 10, 18)


def test_a_free_title_is_recorded_as_free_with_zero_paid():
    items, _ = chirp.parse_purchases_page(_fixture_html())
    wolfs_bane = items[2]
    assert wolfs_bane.is_free is True
    assert wolfs_bane.paid_price == 0.0
    assert wolfs_bane.list_price is None


def test_the_next_page_link_is_reported_when_present():
    _items, next_path = chirp.parse_purchases_page(_fixture_html())
    assert next_path == "/purchases?page=2"


def test_no_next_link_means_the_last_page():
    html = _fixture_html().replace('rel="next"', 'rel="other"')
    _items, next_path = chirp.parse_purchases_page(html)
    assert next_path is None


def _purchases_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def _order(date_text: str, *items: str) -> str:
    return f'<div class="purchase"><div class="purchase-date">{date_text}</div>{"".join(items)}</div>'


def _item(item_id: str, url_path: str, title: str, price_html: str) -> str:
    return (
        f'<div class="purchase-details-body-section purchase-item" data-purchase-item-id="{item_id}">'
        f'<a href="{url_path}"></a><div class="title"><a href="{url_path}">{title}</a></div>'
        f'<div class="pricing-details">{price_html}</div></div>'
    )


async def test_fetch_purchases_walks_pages_and_drops_repeated_items():
    # Page two repeats the first page's Wolf's Bane entry, which must not be counted twice.
    page_two = (
        '<div class="purchases-list">'
        + _order(
            "July 1st, 2024",
            _item("11111", "/audiobooks/old-book-by-someone", "Old Book",
                  '<div class="list-price">$5.00</div><div class="invoice-price">$2.00</div>'),
        )
        + _order(
            "October 18th, 2025",
            _item("26854485", "/audiobooks/wolf-s-bane-by-aimee-easterling", "Wolf's Bane",
                  '<div class="list-price free">FREE!</div>'),
        )
        + "</div>"
    )

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path + ("?" + request.url.query.decode() if request.url.query else ""))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, text=page_two)
        return httpx.Response(200, text=_fixture_html())

    async with _purchases_client(handler) as client:
        purchases = await chirp.fetch_purchases(client)

    assert seen == ["/purchases", "/purchases?page=2"]
    assert [p.item_id for p in purchases] == ["28005047", "28005046", "26854485", "11111"]


async def test_fetch_purchases_reports_a_sign_in_page_as_a_stale_session():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Please sign in</body></html>")

    async with _purchases_client(handler) as client:
        with pytest.raises(chirp.ChirpRequestError, match="stale"):
            await chirp.fetch_purchases(client)


async def test_fetch_purchases_reports_a_cloudflare_challenge():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><title>Just a moment...</title></html>")

    async with _purchases_client(handler) as client:
        with pytest.raises(chirp.ChirpRequestError, match="Cloudflare"):
            await chirp.fetch_purchases(client)


async def test_a_403_on_order_history_says_the_session_likely_expired():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html>Forbidden</html>")

    async with _purchases_client(handler) as client:
        with pytest.raises(chirp.ChirpRequestError, match="403") as excinfo:
            await chirp.fetch_purchases(client)
    assert "cookie" in str(excinfo.value).lower()


async def test_a_403_carrying_a_cloudflare_challenge_is_reported_as_cloudflare():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html><title>Just a moment...</title></html>")

    async with _purchases_client(handler) as client:
        with pytest.raises(chirp.ChirpRequestError, match="Cloudflare"):
            await chirp.fetch_purchases(client)
