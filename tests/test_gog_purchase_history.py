from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


async def test_fetch_order_history_fetches_via_a_real_browser_and_parses_the_response():
    # Confirmed live (2026-10-10): this page's raw server response is
    # Angular's own uncompiled template, never real data — fetch_order_history
    # goes through app.browser's real-headless-browser fetch instead of a
    # plain httpx request, which is what app.browser's own test suite covers
    # in detail. This just confirms fetch_order_history calls it correctly
    # and parses whatever rendered HTML comes back.
    mock_fetch = AsyncMock(return_value=_order_html())
    with patch("app.connectors.gog_connector.fetch_rendered_html", new=mock_fetch):
        items = await gog.fetch_order_history("gog_session=abc")
    assert len(items) == 2
    mock_fetch.assert_awaited_once_with(
        "https://www.gog.com/en/account/settings/orders", "gog_session=abc", gog.GOG_COOKIE_DOMAIN
    )


async def test_fetch_order_history_propagates_a_browser_fetch_failure():
    with patch("app.connectors.gog_connector.fetch_rendered_html", new=AsyncMock(side_effect=RuntimeError("nav failed"))):
        with pytest.raises(RuntimeError):
            await gog.fetch_order_history("stale-cookie")


async def test_fetch_order_history_logs_a_diagnostic_on_zero_orders(caplog):
    # Zero parsed orders from a real rendered page is still ambiguous
    # (genuinely empty vs. a real parsing mismatch) — must stay visible in
    # the log rather than silently returning nothing.
    html = "<html>some shell with no gog-order-item markup</html>"
    with patch("app.connectors.gog_connector.fetch_rendered_html", new=AsyncMock(return_value=html)), caplog.at_level("WARNING"):
        items = await gog.fetch_order_history("gog_session=abc")
    assert items == []
    assert "parsed 0 orders" in caplog.text
    assert "order blocks: 0" in caplog.text


async def test_fetch_order_history_diagnostic_counts_order_blocks_with_no_matching_products(caplog):
    # gog-order-item= wrappers present but zero products parse out of them —
    # e.g. a distributor-sourced order (a bundle-redeemed key activated on
    # GOG) whose product rows don't match the one real, directly-paid order
    # parse_order_history was originally built from.
    html = (
        '<div class="module order-item" gog-order-item="abc123">'
        '<span gog-relative-time="1719792000"></span>'
        '<div class="some-other-shape">no product-row divs here</div>'
        "</div>"
    )
    with patch("app.connectors.gog_connector.fetch_rendered_html", new=AsyncMock(return_value=html)), caplog.at_level("WARNING"):
        items = await gog.fetch_order_history("gog_session=abc")
    assert items == []
    assert "order blocks: 1" in caplog.text
    assert "product-row divs: 0" in caplog.text
