from unittest.mock import AsyncMock, patch

from app.connectors.storefront import StorefrontBundle, StorefrontBundleDetail, StorefrontItem, StorefrontTier
from tests.factories import make_order, make_subproduct


def _bundle(category="games", name="Some Bundle", machine_name="somebundle", start_date=None, end_date=None):
    return StorefrontBundle(
        category=category,
        machine_name=machine_name,
        name=name,
        blurb="Get <em>stuff</em>!",
        product_url=f"https://www.humblebundle.com/{category}/{machine_name}",
        image_url="https://hb.imgix.net/x.jpg",
        start_date=start_date,
        end_date=end_date,
    )


def test_home_requires_auth(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_home_shows_bundles_grouped_by_category(authed_client):
    bundles = [_bundle(category="games", name="Game One"), _bundle(category="books", name="Book One")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)):
        resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "Game One" in resp.text
    assert "Book One" in resp.text
    assert "Games" in resp.text
    assert "Books" in resp.text


def test_home_bundle_tile_image_has_a_fallback_for_a_broken_cover_image(authed_client):
    bundles = [_bundle(category="games", name="Game One")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)):
        resp = authed_client.get("/")
    assert 'onerror="humbleTrackerImgFallback(this)"' in resp.text


def test_home_shows_empty_state_for_category_with_no_bundles(authed_client):
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=[])):
        resp = authed_client.get("/")
    assert "No games bundles currently listed" in resp.text


def test_home_sorts_bundles_newest_first_within_category(authed_client):
    from datetime import datetime

    bundles = [
        _bundle(category="games", name="Oldest", machine_name="oldest", start_date=datetime(2026, 1, 1)),
        _bundle(category="games", name="Newest", machine_name="newest", start_date=datetime(2026, 9, 1)),
        _bundle(category="games", name="Middle", machine_name="middle", start_date=datetime(2026, 5, 1)),
    ]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)):
        resp = authed_client.get("/")
    text = resp.text
    assert text.index("Newest") < text.index("Middle") < text.index("Oldest")


def test_home_sorts_bundles_with_no_start_date_last(authed_client):
    from datetime import datetime

    bundles = [
        _bundle(category="games", name="No Date", machine_name="nodate", start_date=None),
        _bundle(category="games", name="Has Date", machine_name="hasdate", start_date=datetime(2026, 1, 1)),
    ]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)):
        resp = authed_client.get("/")
    text = resp.text
    assert text.index("Has Date") < text.index("No Date")


def test_home_shows_countdown_timer_when_end_date_present(authed_client):
    from datetime import datetime

    bundles = [_bundle(category="games", end_date=datetime(2026, 9, 22, 18, 0, 0))]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)):
        resp = authed_client.get("/")
    assert 'data-countdown-end="2026-09-22T18:00:00Z"' in resp.text


def test_home_omits_countdown_timer_when_no_end_date(authed_client):
    # Note: the page's own JS has a `[data-countdown-end]` CSS selector literal
    # in it regardless (harmless) — check for the real HTML attribute
    # assignment specifically, not just the bare substring.
    bundles = [_bundle(category="games", end_date=None)]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)):
        resp = authed_client.get("/")
    assert 'data-countdown-end="' not in resp.text


def test_storefront_refresh_forces_a_fresh_fetch(authed_client):
    mock_fetch = AsyncMock(return_value=[_bundle()])
    with patch("app.routers.home.storefront.fetch_current_bundles", new=mock_fetch):
        resp = authed_client.post("/storefront/refresh")
    assert resp.status_code == 200
    mock_fetch.assert_awaited_once_with(force=True)


def test_compare_rejects_url_not_on_humblebundle_domain(authed_client):
    resp = authed_client.get("/storefront/compare?url=https://evil.example.com/x")
    assert resp.status_code == 400


def test_compare_rejects_userinfo_host_bypass(authed_client):
    # Classic open-redirect/SSRF host-check bypass: everything before the "@" is
    # userinfo, not the host, so a naive prefix/substring check on the raw string
    # could be fooled into treating this as a humblebundle.com URL. urlparse's
    # .hostname correctly reports "evil.example.com" here.
    resp = authed_client.get("/storefront/compare?url=https://www.humblebundle.com@evil.example.com/x")
    assert resp.status_code == 400


def test_compare_rejects_lookalike_subdomain(authed_client):
    resp = authed_client.get("/storefront/compare?url=https://www.humblebundle.com.evil.example.com/x")
    assert resp.status_code == 400


def test_compare_rejects_non_https_scheme(authed_client):
    resp = authed_client.get("/storefront/compare?url=http://www.humblebundle.com/x")
    assert resp.status_code == 400


def test_compare_shows_owned_vs_new_items(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Owned Item", machine_name="owned")]))

    detail = StorefrontBundleDetail(
        name="For-Sale Bundle",
        msrp_total=50.0,
        items=[
            StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=10.0),
            StorefrontItem(machine_name="new_item", name="New Item", content_type="ebook", msrp_amount=15.0),
        ],
        tiers=[
            StorefrontTier(
                identifier="initial",
                label="Pay $5 to unlock!",
                price_amount=5.0,
                is_bta=False,
                items=[
                    StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=10.0),
                    StorefrontItem(machine_name="new_item", name="New Item", content_type="ebook", msrp_amount=15.0),
                ],
            )
        ],
    )
    with patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=detail)):
        resp = authed_client.get("/storefront/compare?url=https://www.humblebundle.com/books/x")

    assert resp.status_code == 200
    assert "1 of 2 item(s) already in your catalog" in resp.text
    assert "Already owned" in resp.text
    assert "New to you" in resp.text


def test_compare_gives_every_tier_table_the_same_alignment_class(authed_client, make_bundle):
    # Regression: each tier renders its own <table>, auto-sized independently
    # by default — Type/Value/Status only line up across tiers if every one
    # of them shares the same fixed column-width class.
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Owned Item", machine_name="owned")]))

    detail = StorefrontBundleDetail(
        name="For-Sale Bundle",
        msrp_total=50.0,
        items=[StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=10.0)],
        tiers=[
            StorefrontTier(
                identifier="tier1",
                label="Pay $5 to unlock!",
                price_amount=5.0,
                is_bta=False,
                items=[StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=10.0)],
            ),
            StorefrontTier(
                identifier="tier2",
                label="Pay $10 or more to also unlock!",
                price_amount=10.0,
                is_bta=False,
                items=[StorefrontItem(machine_name="new_item", name="A Much Longer Item Name Here", content_type="ebook", msrp_amount=20.0)],
            ),
        ],
    )
    with patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=detail)):
        resp = authed_client.get("/storefront/compare?url=https://www.humblebundle.com/books/x")

    assert resp.text.count('class="compare-table"') == 2


def test_compare_shows_avg_paid_for_owned_items_not_msrp(authed_client, make_bundle):
    make_bundle(
        gamekey="GK1",
        order=make_order(amount_spent=18.0, subproducts=[make_subproduct("Owned Item", machine_name="owned")]),
    )
    detail = StorefrontBundleDetail(
        name="For-Sale Bundle",
        msrp_total=None,
        items=[StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=999.0)],
        tiers=[
            StorefrontTier(
                identifier="initial",
                label="Pay $5",
                price_amount=5.0,
                is_bta=False,
                items=[StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=999.0)],
            )
        ],
    )
    with patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=detail)):
        resp = authed_client.get("/storefront/compare?url=https://www.humblebundle.com/books/x")

    assert "avg paid" in resp.text
    assert "999.00" not in resp.text  # MSRP must not be shown once the item is owned


def test_compare_shows_multi_bundle_button_for_duplicates(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Dup", machine_name="dup")]))
    make_bundle(gamekey="GK2", order=make_order(subproducts=[make_subproduct("Dup", machine_name="dup")]))
    detail = StorefrontBundleDetail(
        name="B",
        msrp_total=None,
        items=[StorefrontItem(machine_name="dup", name="Dup", content_type="ebook", msrp_amount=None)],
        tiers=[
            StorefrontTier(
                identifier="initial", label="Pay $5", price_amount=5.0, is_bta=False,
                items=[StorefrontItem(machine_name="dup", name="Dup", content_type="ebook", msrp_amount=None)],
            )
        ],
    )
    with patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=detail)):
        resp = authed_client.get("/storefront/compare?url=https://www.humblebundle.com/books/x")
    # Raw HTML source has the literal entity, not the Unicode character a browser
    # would render it as.
    assert "&times;2" in resp.text
    assert "/catalog/item/dup/bundles" in resp.text
