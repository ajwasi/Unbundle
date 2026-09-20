from unittest.mock import AsyncMock, patch

import nh3
import pytest

from app.connectors.storefront import StorefrontBundle, StorefrontBundleDetail, StorefrontItem, StorefrontTier
from tests.factories import make_order, make_subproduct

# Home fetches every listed bundle's detail (for the owned-ratio) concurrently
# with the listing itself — any test with a non-empty bundle list needs this
# mocked too, or it'd attempt a real (sandbox-blocked) network call. This
# stand-in has zero items, so _build_tiles() reports total_count=0 (falsy),
# keeping the ratio line suppressed and every pre-existing assertion below
# unaffected. Tests with an empty bundle list don't need this at all —
# asyncio.gather() over zero tasks never calls fetch_bundle_detail.
_EMPTY_DETAIL = StorefrontBundleDetail(name="", msrp_total=None, items=[], tiers=[])


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
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "Game One" in resp.text
    assert "Book One" in resp.text
    assert "Games" in resp.text
    assert "Books" in resp.text


def test_home_shows_not_refreshed_yet_when_never_fetched(authed_client):
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=[])), \
         patch("app.routers.home.storefront.last_fetched_at", return_value=None):
        resp = authed_client.get("/")
    assert "Not refreshed yet" in resp.text


def test_home_shows_last_refreshed_time(authed_client):
    from datetime import datetime, timedelta

    fetched_at = datetime.utcnow() - timedelta(minutes=30)
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=[])), \
         patch("app.routers.home.storefront.last_fetched_at", return_value=fetched_at):
        resp = authed_client.get("/")
    assert "Last refreshed" in resp.text
    assert "30 minutes ago" in resp.text


def test_home_bundle_tile_image_has_a_fallback_for_a_broken_cover_image(authed_client):
    bundles = [_bundle(category="games", name="Game One")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert 'onerror="unbundleImgFallback(this)"' in resp.text


def test_home_shows_empty_state_when_no_bundles_in_any_category(authed_client):
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=[])):
        resp = authed_client.get("/")
    assert "Nothing currently listed for sale" in resp.text


def test_home_collapses_empty_categories_when_others_have_bundles(authed_client):
    # Only "games" has a bundle — "books"/"software"/etc. categories should
    # be skipped entirely rather than each rendering their own empty card.
    bundles = [_bundle(category="games", name="Game One")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert "Game One" in resp.text
    assert "currently listed" not in resp.text


def test_home_sorts_bundles_newest_first_within_category(authed_client):
    from datetime import datetime

    bundles = [
        _bundle(category="games", name="Oldest", machine_name="oldest", start_date=datetime(2026, 1, 1)),
        _bundle(category="games", name="Newest", machine_name="newest", start_date=datetime(2026, 9, 1)),
        _bundle(category="games", name="Middle", machine_name="middle", start_date=datetime(2026, 5, 1)),
    ]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    text = resp.text
    assert text.index("Newest") < text.index("Middle") < text.index("Oldest")


def test_home_sorts_bundles_with_no_start_date_last(authed_client):
    from datetime import datetime

    bundles = [
        _bundle(category="games", name="No Date", machine_name="nodate", start_date=None),
        _bundle(category="games", name="Has Date", machine_name="hasdate", start_date=datetime(2026, 1, 1)),
    ]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    text = resp.text
    assert text.index("Has Date") < text.index("No Date")


def test_home_shows_countdown_timer_when_end_date_present(authed_client):
    from datetime import datetime

    bundles = [_bundle(category="games", end_date=datetime(2026, 9, 22, 18, 0, 0))]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert 'data-countdown-end="2026-09-22T18:00:00Z"' in resp.text


def test_home_omits_countdown_timer_when_no_end_date(authed_client):
    # Note: the page's own JS has a `[data-countdown-end]` CSS selector literal
    # in it regardless (harmless) — check for the real HTML attribute
    # assignment specifically, not just the bare substring.
    bundles = [_bundle(category="games", end_date=None)]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert 'data-countdown-end="' not in resp.text


def test_storefront_refresh_forces_a_fresh_fetch(authed_client):
    mock_fetch = AsyncMock(return_value=[_bundle()])
    with patch("app.routers.home.storefront.fetch_current_bundles", new=mock_fetch), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.post("/storefront/refresh")
    assert resp.status_code == 200
    mock_fetch.assert_awaited_once_with(force=True)


def test_home_shows_owned_ratio_for_a_bundle(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Owned Item", machine_name="owned")]))
    bundles = [_bundle(category="games", name="Game One")]
    detail = StorefrontBundleDetail(
        name="Game One",
        msrp_total=None,
        items=[
            StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=None),
            StorefrontItem(machine_name="new_item", name="New Item", content_type="ebook", msrp_amount=None),
        ],
        tiers=[],
    )
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=detail)):
        resp = authed_client.get("/")
    assert "1/2 owned" in resp.text
    assert 'class="badge badge-pending">1/2 owned' in resp.text  # partial, not fully owned


def test_home_owned_ratio_badge_is_ok_when_fully_owned(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Owned Item", machine_name="owned")]))
    bundles = [_bundle(category="games", name="Game One")]
    detail = StorefrontBundleDetail(
        name="Game One",
        msrp_total=None,
        items=[StorefrontItem(machine_name="owned", name="Owned Item", content_type="ebook", msrp_amount=None)],
        tiers=[],
    )
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=detail)):
        resp = authed_client.get("/")
    assert 'class="badge badge-ok">1/1 owned' in resp.text


def test_home_marks_a_bundle_already_purchased_by_machine_name(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(machine_name="samebundle"))
    bundles = [_bundle(category="games", name="Game One", machine_name="samebundle")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert "Already purchased" in resp.text
    assert 'class="bundle-tile already-purchased"' in resp.text
    assert 'href="/bundles/GK1"' in resp.text


def test_home_does_not_flag_already_purchased_for_a_different_machine_name(authed_client, make_bundle):
    make_bundle(gamekey="GK1", order=make_order(machine_name="someotherbundle"))
    bundles = [_bundle(category="games", name="Game One", machine_name="samebundle")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert "Already purchased" not in resp.text
    assert "already-purchased" not in resp.text


def test_home_ignores_blank_machine_names_when_matching_purchased(authed_client, make_bundle):
    # A bundle synced before the machine_name column existed has "" for it —
    # must never register as a false-positive match against a storefront
    # listing that (in some edge case) also resolved to a blank machine_name.
    make_bundle(gamekey="GK1", order=make_order(machine_name=""))
    bundles = [_bundle(category="games", name="Game One", machine_name="")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_EMPTY_DETAIL)):
        resp = authed_client.get("/")
    assert "Already purchased" not in resp.text


def test_home_degrades_gracefully_when_a_bundle_detail_fetch_fails(authed_client):
    bundles = [_bundle(category="games", name="Game One")]
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=bundles)), \
         patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(side_effect=ValueError("boom"))):
        resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "Game One" in resp.text
    assert "owned</span>" not in resp.text  # no ratio badge for a bundle whose detail fetch failed


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


def _connect_humble(db):
    from app.models.credential import SOURCE_HUMBLE, STATUS_OK, Credential
    from app.security import encrypt_json

    db.add(Credential(source=SOURCE_HUMBLE, status=STATUS_OK, encrypted_payload=encrypt_json({"session_key": "x"})))
    db.commit()


def test_home_shows_getting_started_banner_before_humble_is_connected(authed_client):
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=[])):
        resp = authed_client.get("/")
    assert "Welcome to Unbundle" in resp.text
    assert 'href="/settings"' in resp.text


def test_home_hides_getting_started_banner_once_humble_is_connected(authed_client, db):
    _connect_humble(db)
    with patch("app.routers.home.storefront.fetch_current_bundles", new=AsyncMock(return_value=[])):
        resp = authed_client.get("/")
    assert "Welcome to Unbundle" not in resp.text


# ------------------------------------------- richer compare-page item detail

def _detailed_item():
    return StorefrontItem(
        machine_name="book1",
        name="No One Else",
        content_type="ebook",
        msrp_amount=19.99,
        description="<p>A graphic novel.</p>",
        authors=["R. Kikuo Johnson"],
        publishers=["Fantagraphics"],
        formats=["PDF", "EPUB"],
        delivery_methods=["DRM-free download"],
        image_url="https://hb.imgix.net/cover.png?w=180",
        image_url_2x="https://hb.imgix.net/cover.png?dpr=2",
    )


def _detail_with(item):
    return StorefrontBundleDetail(
        name="A Bundle",
        msrp_total=None,
        items=[item],
        tiers=[StorefrontTier(identifier="t1", label="Pay $5", price_amount=5.0, is_bta=False, items=[item])],
    )


def _compare(authed_client, item, **params):
    with patch("app.routers.home.storefront.fetch_bundle_detail", new=AsyncMock(return_value=_detail_with(item))):
        return authed_client.get(
            "/storefront/compare", params={"url": "https://www.humblebundle.com/books/x", **params}
        )


def test_compare_list_view_shows_cover_author_publisher_and_formats(authed_client):
    resp = _compare(authed_client, _detailed_item())

    assert "R. Kikuo Johnson" in resp.text
    assert "Fantagraphics" in resp.text
    assert "PDF" in resp.text
    assert "DRM-free download" in resp.text
    assert "hb.imgix.net/cover.png" in resp.text
    assert "A graphic novel." in resp.text


def test_compare_defaults_to_the_list_view(authed_client):
    resp = _compare(authed_client, _detailed_item())
    assert "compare-table" in resp.text
    assert 'class="item-grid"' not in resp.text


def test_compare_grid_view_renders_cards_instead_of_a_table(authed_client):
    resp = _compare(authed_client, _detailed_item(), view="grid")

    assert 'class="item-grid"' in resp.text
    assert "compare-table" not in resp.text
    # Same details must survive the switch.
    assert "R. Kikuo Johnson" in resp.text
    assert "DRM-free download" in resp.text


def test_an_unknown_view_value_falls_back_to_the_list(authed_client):
    resp = _compare(authed_client, _detailed_item(), view="carousel")
    assert "compare-table" in resp.text


def test_both_views_offer_a_link_to_the_other(authed_client):
    resp = _compare(authed_client, _detailed_item())
    assert "view=grid" in resp.text
    assert "view=list" in resp.text


def test_an_item_missing_the_extra_fields_still_renders(authed_client):
    # Older cached details and the charity-tile shape carry none of this.
    bare = StorefrontItem(machine_name="x", name="Bare Item", content_type="ebook", msrp_amount=None)
    resp = _compare(authed_client, bare)

    assert resp.status_code == 200
    assert "Bare Item" in resp.text


def test_description_html_is_sanitized_before_it_reaches_the_page():
    from app.connectors import storefront

    dirty = '<p>Fine</p><script>alert(1)</script><a href="javascript:alert(1)" onerror="x">link</a>'
    clean = nh3.clean(dirty, tags=storefront._ALLOWED_DESCRIPTION_TAGS, attributes={})

    assert "<script" not in clean
    assert "javascript:" not in clean
    assert "onerror" not in clean
    assert "<p>Fine</p>" in clean


# ------------------------------------------------- storefront item art keys

@pytest.mark.parametrize(
    "resolved,expected_url,expected_2x",
    [
        # Books and comics: the portrait key art, with its own retina pair.
        (
            {"front_page_art_imgix": "a.png", "front_page_art_imgix_retina": "a2.png", "featured_image": "f.png"},
            "a.png",
            "a2.png",
        ),
        # Games frequently null that key and populate featured_image instead,
        # which is why half a games bundle used to render with no covers.
        ({"front_page_art_imgix": None, "featured_image": "f.png"}, "f.png", ""),
        ({"front_page_art_imgix": "", "featured_image": "", "preview_image": "p.png"}, "p.png", ""),
        ({}, "", ""),
        ({"front_page_art_imgix": None, "featured_image": None, "preview_image": None}, "", ""),
    ],
)
def test_item_art_falls_back_across_the_keys_humble_actually_uses(resolved, expected_url, expected_2x):
    from app.connectors import storefront

    images = storefront._item_images(resolved)
    assert images["image_url"] == expected_url
    assert images["image_url_2x"] == expected_2x


def test_a_retina_url_is_never_paired_with_a_different_images_one_x():
    # front_page_art_imgix_retina belongs to front_page_art_imgix. Carrying it
    # over to a featured_image fallback would serve two unrelated pictures
    # depending on screen density.
    from app.connectors import storefront

    images = storefront._item_images(
        {"front_page_art_imgix": None, "front_page_art_imgix_retina": "wrong2x.png", "featured_image": "f.png"}
    )
    assert images["image_url"] == "f.png"
    assert images["image_url_2x"] == ""
