from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import gog_connector as gog

FIXTURES = Path(__file__).parent / "fixtures"


def _order_html() -> str:
    return (FIXTURES / "gog_order_history_order.html").read_text(encoding="utf-8")


def _paid_order_html(order_id="paid1", title="Firewatch", price="4.23", unix_ts="1719792000") -> str:
    # A minimal, hand-built second order in the same real shape, to cover a
    # non-free, non-zero price — the one real capture this fixture is based
    # on happened to be a $0.00 (Free) order.
    return (
        f'<div class="module order-item" gog-order-item="{order_id}">'
        f'<span gog-relative-time="{unix_ts}" data-cy="order-date">x</span>'
        f'<div class="product-state-holder product-row  order-product" ng-repeat="product in order.products">'
        f'<span class="_price" ng-bind="product.price.amount" data-cy="product-price">{price}</span>'
        f'<span class="product-title__text" ng-bind="::product.title" hook-test="productTitle">{title}</span>'
        f'</div></div>'
    )


def test_parse_order_history_reads_every_product_in_an_order():
    items = gog.parse_order_history(_order_html())
    assert [i.title for i in items] == ["CAYNE", "Dagon: by H. P. Lovecraft"]
    assert all(i.price_amount == 0.0 for i in items)
    assert all(i.purchased_at == datetime(2026, 7, 1, 23, 54, 12) for i in items)


def test_parse_order_history_reads_a_real_price_and_separates_orders():
    html = _order_html() + _paid_order_html()
    items = gog.parse_order_history(html)
    firewatch = next(i for i in items if i.title == "Firewatch")
    assert firewatch.price_amount == 4.23
    assert firewatch.purchased_at == datetime(2024, 7, 1)


def test_parse_order_history_skips_a_truncated_trailing_product():
    html = _order_html() + '<div class="module order-item" gog-order-item="partial"><span gog-relative-time="1719792000"></span><div class="product-state-holder product-row  order-product" ng-repeat="product in order.products"><span class="_price" data-cy="product-price">1.00</span>'
    items = gog.parse_order_history(html)
    assert [i.title for i in items] == ["CAYNE", "Dagon: by H. P. Lovecraft"]


def test_parse_order_history_handles_no_orders():
    assert gog.parse_order_history("<html>no orders here</html>") == []


async def test_fetch_order_history_sends_the_cookie_and_parses_the_response():
    resp = httpx.Response(200, text=_order_html(), request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        items = await gog.fetch_order_history("gog_session=abc")
    assert len(items) == 2
    assert mock_get.call_args.kwargs["headers"]["Cookie"] == "gog_session=abc"


async def test_fetch_order_history_raises_on_a_bad_status():
    resp = httpx.Response(403, text="nope", request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(gog.GogAuthError):
            await gog.fetch_order_history("stale-cookie")


async def test_fetch_order_history_logs_a_diagnostic_on_a_200_with_zero_orders(caplog):
    # A 200 with nothing parsed is ambiguous (truly empty vs. a page whose
    # order list only exists after client-side JS runs, which a plain GET
    # never executes) — this must be visible in the log either way, never
    # silent, since the fix differs completely depending on which it is.
    resp = httpx.Response(200, text="<html>some shell with no gog-order-item markup</html>", request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)), caplog.at_level("WARNING"):
        items = await gog.fetch_order_history("gog_session=abc")
    assert items == []
    assert "parsed 0 orders" in caplog.text
    assert "has gog-order-item markup: False" in caplog.text
