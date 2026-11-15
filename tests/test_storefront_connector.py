import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.connectors.storefront as storefront


@pytest.fixture(autouse=True)
def _reset_caches():
    # Module-level TTL caches must not leak between tests.
    storefront._listing_cache = None
    storefront._detail_cache = {}
    storefront._listing_fetched_at = None
    yield
    storefront._listing_cache = None
    storefront._detail_cache = {}
    storefront._listing_fetched_at = None


def _listing_html(products_by_category: dict) -> str:
    data = {
        cat: {"mosaic": [{"products": products}]}
        for cat, products in products_by_category.items()
    }
    payload = {"data": data}
    return f'<html><script id="landingPage-json-data" type="application/json">{json.dumps(payload)}</script></html>'


def _detail_html(bundle_data: dict) -> str:
    payload = {"bundleData": bundle_data}
    return f'<html><script id="webpack-bundle-page-data" type="application/json">{json.dumps(payload)}</script></html>'


def _product(
    name="Some Bundle",
    machine_name="somebundle",
    product_url="/games/some-bundle",
    category="bundle",
    start_date="2026-09-01T18:00:00",
    end_date="2026-09-22T18:00:00",
):
    product = {
        "machine_name": machine_name,
        "tile_name": name,
        "marketing_blurb": "Get <em>Cool Thing</em>!",
        "product_url": product_url,
        "tile_image": "https://hb.imgix.net/img.jpg",
        "category": category,
    }
    if start_date is not None:
        product["start_date|datetime"] = start_date
    if end_date is not None:
        product["end_date|datetime"] = end_date
    return product


def test_last_fetched_at_is_none_before_any_fetch():
    assert storefront.last_fetched_at() is None


@pytest.mark.asyncio
async def test_last_fetched_at_is_set_after_a_real_fetch():
    html = _listing_html({"games": [], "books": [], "software": []})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        await storefront.fetch_current_bundles()
    assert storefront.last_fetched_at() is not None


@pytest.mark.asyncio
async def test_last_fetched_at_unchanged_on_a_cache_hit():
    html = _listing_html({"games": [], "books": [], "software": []})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)) as mock_get:
        await storefront.fetch_current_bundles()
        first = storefront.last_fetched_at()
        await storefront.fetch_current_bundles()  # within TTL — cache hit, no real fetch
        assert mock_get.call_count == 1
    assert storefront.last_fetched_at() == first


@pytest.mark.asyncio
async def test_fetch_current_bundles_parses_all_three_categories():
    html = _listing_html({"games": [_product("Game Bundle", "gb", "/games/gb")], "books": [_product("Book Bundle", "bb", "/books/bb")], "software": []})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert {b.category for b in bundles} == {"games", "books"}
    game = next(b for b in bundles if b.category == "games")
    assert game.name == "Game Bundle"
    assert game.machine_name == "gb"
    assert game.product_url == "https://www.humblebundle.com/games/gb"
    assert "<em>Cool Thing</em>" in game.blurb


@pytest.mark.asyncio
async def test_fetch_current_bundles_sanitizes_blurb_markup():
    # blurb renders with autoescaping bypassed (see home/_bundles.html), so a
    # compromised or malicious listing must not be able to inject script/markup
    # through it even though this field is otherwise trusted.
    # Note: deliberately no literal "</script>" substring in the payload below —
    # that would prematurely terminate the *test fixture's* naive regex-based
    # extraction of the surrounding <script id="landingPage-json-data"> block
    # (a test-harness limitation, unrelated to nh3's real handling of <script>,
    # which was separately confirmed interactively).
    product = _product()
    product["marketing_blurb"] = 'Buy <a href="javascript:alert(1)">now</a><img src=x onerror=alert(1)>!'
    html = _listing_html({"games": [product]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    blurb = bundles[0].blurb
    assert "javascript:" not in blurb
    assert "onerror" not in blurb
    assert "<a" not in blurb and "<img" not in blurb
    assert "Buy" in blurb and "now" in blurb  # stripped tags still keep their visible text


@pytest.mark.asyncio
async def test_fetch_current_bundles_parses_start_and_end_dates():
    from datetime import datetime

    html = _listing_html({"games": [_product(start_date="2026-08-26T18:00:00", end_date="2026-09-17T04:00:00")]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles[0].start_date == datetime(2026, 8, 26, 18, 0, 0)
    assert bundles[0].end_date == datetime(2026, 9, 17, 4, 0, 0)


@pytest.mark.asyncio
async def test_fetch_current_bundles_handles_missing_dates():
    html = _listing_html({"games": [_product(start_date=None, end_date=None)]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles[0].start_date is None
    assert bundles[0].end_date is None


@pytest.mark.asyncio
async def test_fetch_current_bundles_handles_malformed_dates():
    html = _listing_html({"games": [_product(end_date="not-a-real-date")]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles[0].end_date is None  # parsed defensively, never raises


@pytest.mark.asyncio
async def test_fetch_current_bundles_skips_non_bundle_products():
    html = _listing_html({"games": [_product(category="storefront")]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        bundles = await storefront.fetch_current_bundles()
    assert bundles == []


@pytest.mark.asyncio
async def test_fetch_current_bundles_raises_when_marker_missing():
    resp = httpx.Response(200, text="<html>no data here</html>", request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        with pytest.raises(ValueError):
            await storefront.fetch_current_bundles()


@pytest.mark.asyncio
async def test_fetch_current_bundles_uses_cache_within_ttl():
    html = _listing_html({"games": [_product()]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await storefront.fetch_current_bundles()
        await storefront.fetch_current_bundles()
    assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_current_bundles_force_bypasses_cache():
    html = _listing_html({"games": [_product()]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await storefront.fetch_current_bundles()
        await storefront.fetch_current_bundles(force=True)
    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_current_bundles_refetches_after_ttl_expires():
    html = _listing_html({"games": [_product()]})
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await storefront.fetch_current_bundles()
        storefront._listing_cache = (storefront._listing_cache[0] - storefront.LISTING_TTL_SECONDS - 1, storefront._listing_cache[1])
        await storefront.fetch_current_bundles()
    assert mock_get.call_count == 2


def _tier_item(machine_name, human_name, content_type="ebook", price=10.0):
    return {"human_name": human_name, "item_content_type": content_type, "min_price|money": {"amount": price}}


@pytest.mark.asyncio
async def test_fetch_bundle_detail_rejects_a_url_on_another_host():
    # Defense-in-depth: home.py's /storefront/compare route already validates the
    # host before ever calling this, but this connector must not blindly trust an
    # arbitrary caller (or a future one) to have done that.
    with patch("httpx.AsyncClient.get", new=AsyncMock()) as mock_get:
        with pytest.raises(ValueError, match="outside"):
            await storefront.fetch_bundle_detail("https://evil.example.com/books/x")
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_bundle_detail_rejects_non_https_scheme():
    with patch("httpx.AsyncClient.get", new=AsyncMock()) as mock_get:
        with pytest.raises(ValueError, match="outside"):
            await storefront.fetch_bundle_detail("http://www.humblebundle.com/books/x")
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_bundle_detail_preserves_query_and_fragment():
    bundle_data = {"basic_data": {}, "tier_item_data": {}, "tier_order": [], "tier_display_data": {}, "tier_pricing_data": {}}
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)) as mock_get:
        await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x?ref=1#top")
    mock_get.assert_awaited_once_with("https://www.humblebundle.com/books/x?ref=1#top")


@pytest.mark.asyncio
async def test_fetch_bundle_detail_requests_a_url_rebuilt_from_base_url_not_reused_verbatim():
    # The actual fix, not just the validation: client.get() must never receive the
    # original product_url string, even a validated one — only a URL rebuilt from
    # BASE_URL (a trusted literal) plus the path/query/fragment pulled out of it.
    # A same-URL-either-way input can't tell these apart, so this uses a host with
    # different case: urlparse().hostname lowercases for comparison (so this still
    # passes validation), but naively reusing product_url verbatim would send the
    # mixed-case string to client.get() instead of BASE_URL's canonical lowercase one.
    bundle_data = {"basic_data": {}, "tier_item_data": {}, "tier_order": [], "tier_display_data": {}, "tier_pricing_data": {}}
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)) as mock_get:
        await storefront.fetch_bundle_detail("https://WWW.HUMBLEBUNDLE.COM/books/x")
    mock_get.assert_awaited_once_with("https://www.humblebundle.com/books/x")


@pytest.mark.asyncio
async def test_fetch_bundle_detail_parses_name_and_msrp():
    bundle_data = {
        "basic_data": {"human_name": "My Bundle", "msrp|money": {"amount": 100.0}},
        "tier_item_data": {},
        "tier_order": [],
        "tier_display_data": {},
        "tier_pricing_data": {},
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")
    assert detail.name == "My Bundle"
    assert detail.msrp_total == 100.0


@pytest.mark.asyncio
async def test_fetch_bundle_detail_filters_out_charity_tip_items():
    # A charity/EFF-style tile has a human_name but no item_content_type — not a
    # real product, must not appear as a comparable item.
    bundle_data = {
        "basic_data": {"human_name": "B"},
        "tier_item_data": {
            "eff": {"human_name": "Electronic Frontier Foundation", "item_content_type": None},
            "realbook": _tier_item("realbook", "Real Book"),
        },
        "tier_order": [],
        "tier_display_data": {},
        "tier_pricing_data": {},
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")
    assert [i.machine_name for i in detail.items] == ["realbook"]


@pytest.mark.asyncio
async def test_fetch_bundle_detail_tier_diffing_shows_only_newly_unlocked_items():
    # Confirmed against a real 3-tier bundle: tier_item_machine_names is
    # CUMULATIVE (each tier repeats every lower tier's items plus its own new
    # ones) — each StorefrontTier must only list what's new at that price.
    bundle_data = {
        "basic_data": {"human_name": "Tiered Bundle"},
        "tier_item_data": {
            "a": _tier_item("a", "Item A"),
            "b": _tier_item("b", "Item B"),
            "c": _tier_item("c", "Item C"),
        },
        "tier_order": ["bt25", "initial"],  # highest-price-first, as Humble returns it
        "tier_display_data": {
            "initial": {"header": "Pay $5 to unlock!", "tier_item_machine_names": ["a"], "bonus_item_machine_names": []},
            "bt25": {
                "header": "Pay $25 or more to also unlock!",
                "tier_item_machine_names": ["a", "b", "c"],
                "bonus_item_machine_names": [],
            },
        },
        "tier_pricing_data": {
            "initial": {"price|money": {"amount": 5.0}, "is_bta": False},
            "bt25": {"price|money": {"amount": 25.0}, "is_bta": False},
        },
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")

    assert [t.identifier for t in detail.tiers] == ["initial", "bt25"]  # reordered low-to-high
    assert [i.machine_name for i in detail.tiers[0].items] == ["a"]
    assert [i.machine_name for i in detail.tiers[1].items] == ["b", "c"]  # "a" not repeated
    assert detail.tiers[0].price_amount == 5.0
    assert detail.tiers[1].price_amount == 25.0


@pytest.mark.asyncio
async def test_fetch_bundle_detail_skips_tier_with_nothing_new():
    # A tier whose only "new" item was the already-filtered-out charity tile
    # should not produce an empty tier card.
    bundle_data = {
        "basic_data": {"human_name": "B"},
        "tier_item_data": {"a": _tier_item("a", "Item A"), "eff": {"human_name": "EFF", "item_content_type": None}},
        "tier_order": ["bt25", "initial"],
        "tier_display_data": {
            "initial": {"header": "Pay $5", "tier_item_machine_names": ["a"], "bonus_item_machine_names": []},
            "bt25": {"header": "Pay $25", "tier_item_machine_names": ["a", "eff"], "bonus_item_machine_names": []},
        },
        "tier_pricing_data": {
            "initial": {"price|money": {"amount": 5.0}, "is_bta": False},
            "bt25": {"price|money": {"amount": 25.0}, "is_bta": False},
        },
    }
    resp = httpx.Response(200, text=_detail_html(bundle_data), request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x")
    assert [t.identifier for t in detail.tiers] == ["initial"]


def _mock_get(html: str):
    resp = httpx.Response(200, text=html, request=httpx.Request("GET", f"{storefront.BASE_URL}/bundles"))
    return patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp))


@pytest.mark.asyncio
async def test_listing_survives_renamed_script_id_and_extra_attributes():
    payload = {"data": {"games": {"mosaic": [{"products": [_product()]}]}}}
    html = f'<html><script nonce="x" type="application/json" id="renamed-data">{json.dumps(payload)}</script></html>'
    with _mock_get(html):
        bundles = await storefront.fetch_current_bundles(force=True)
    assert [b.machine_name for b in bundles] == ["somebundle"]


@pytest.mark.asyncio
async def test_listing_failure_reports_page_diagnostics():
    html = '<html><title>Just a moment...</title><script id="cf-chl">var a=1;</script></html>'
    with _mock_get(html):
        with pytest.raises(ValueError, match=r"Just a moment.*cf-chl"):
            await storefront.fetch_current_bundles(force=True)


# --- Next.js flight-stream pages (Humble's site since Nov 2026) -----------------


def _compact(obj) -> str:
    # Humble's flight stream is compact JSON; the parsers locate objects by exact marker text.
    return json.dumps(obj, separators=(",", ":"))


def _flight_html(*chunks: str) -> str:
    """A page whose data lives only in self.__next_f.push([1, "..."]) calls."""
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks)
    return f"<html><head><title>All Bundles</title></head><body>{scripts}</body></html>"


def _hit(slug="data-science-oreilly-books", category="books", **overrides):
    value = {
        "id": "abc",
        "productKey": "datascienceoreilly_bookbundle",
        "slug": slug,
        "url": f"/{category}/{slug}",
        "title": "Data Science by O'Reilly",
        "thumbnail": "https://hb-prd.imgix.net/product-media/t.jpg",
        "endDate": "2026-11-16T19:00:00.000Z",
        "category": category,
        "itemsCount": 5,
        "productType": "humble-bundle",
        "includedItemTitles": ["Book A", "Book B", "Book <script>x</script>", "Book D"],
    }
    value.update(overrides)
    return {"relationTo": "algolia-hit", "value": value}


def _item(key, title, **overrides):
    item = {
        "id": key,
        "key": key,
        "title": title,
        "thumbnailImageUrl": f"https://hb-prd.imgix.net/{key}.png",
        "description": "$70",
        "developers": [{"name": "Dev One", "url": "http://x"}],
        "publishers": [{"name": "Pub One"}],
        "deliveryMethods": ["download"],
        "platforms": ["pdf", "epub"],
        "bundleType": "books",
        "msrp": "$undefined",
    }
    item.update(overrides)
    return item


def _bundle_page(tiers):
    return {"bundlePage": {"slug": "x", "title": "My Flight Bundle", "bundleType": "books", "value": {"amount": 1597, "currencyCode": "USD"}, "tiers": tiers}}


def _flight_page_chunk(page: dict) -> str:
    return 'e:[["$","section",null,' + _compact({"children": ["$", "$L6e", None, page]}) + "]]"


@pytest.mark.asyncio
async def test_listing_parses_next_js_flight_data():
    chunk = "5:" + _compact([_hit(), _hit(slug="game-one", category="games", title="Game One", productKey="gameone")])
    with _mock_get(_flight_html(chunk)):
        bundles = await storefront.fetch_current_bundles(force=True)

    assert [b.category for b in bundles] == ["books", "games"]
    first = bundles[0]
    assert first.machine_name == "datascienceoreilly_bookbundle"
    assert first.name == "Data Science by O'Reilly"
    assert first.product_url == "https://www.humblebundle.com/books/data-science-oreilly-books"
    assert first.image_url == "https://hb-prd.imgix.net/product-media/t.jpg"
    # Naive UTC, like the legacy dates — the template appends its own "Z".
    assert first.end_date == datetime(2026, 11, 16, 19, 0)
    assert first.end_date.tzinfo is None
    assert first.start_date is None


@pytest.mark.asyncio
async def test_flight_listing_blurb_is_sanitized_and_summarizes_contents():
    with _mock_get(_flight_html("5:" + _compact([_hit()]))):
        (bundle,) = await storefront.fetch_current_bundles(force=True)
    assert bundle.blurb.startswith("Includes Book A, Book B, ")
    assert bundle.blurb.endswith("and more")
    assert "<script" not in bundle.blurb


@pytest.mark.asyncio
async def test_flight_listing_ignores_non_bundles_and_duplicate_urls():
    hits = [_hit(), _hit(), _hit(slug="choice", productType="humble-choice")]
    with _mock_get(_flight_html("5:" + _compact(hits))):
        bundles = await storefront.fetch_current_bundles(force=True)
    assert [b.product_url.rsplit("/", 1)[1] for b in bundles] == ["data-science-oreilly-books"]


@pytest.mark.asyncio
async def test_flight_listing_reads_data_split_across_several_pushes():
    text = "5:" + _compact([_hit()])
    cut = len(text) // 2
    with _mock_get(_flight_html(text[:cut], text[cut:])):
        bundles = await storefront.fetch_current_bundles(force=True)
    assert len(bundles) == 1


@pytest.mark.asyncio
async def test_flight_listing_with_no_bundles_reports_page_diagnostics():
    with _mock_get(_flight_html('1:"$Sreact.fragment"')):
        with pytest.raises(ValueError, match=r"All Bundles"):
            await storefront.fetch_current_bundles(force=True)


@pytest.mark.asyncio
async def test_detail_parses_next_js_flight_data_with_tiers_and_back_references():
    tier1 = {"id": "1", "price": {"amount": 5}, "bundleItems": [_item("a_x", "Book A"), _item("b_x", "Book B")]}
    # Higher tiers repeat lower tiers' items as string references; only the dict is new.
    tier2 = {"id": "2", "price": {"amount": 18}, "bundleItems": ["$e:0:props:bundlePage:tiers:0:bundleItems:0", _item("c_x", "Book C")]}
    tier3 = {"id": "3", "price": {"amount": 30}, "bundleItems": ["$e:0:props:bundlePage:tiers:0:bundleItems:1"]}
    html = _flight_html(_flight_page_chunk(_bundle_page([tier2, tier3, tier1])))  # deliberately out of order
    with _mock_get(html):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x", force=True)

    assert detail.name == "My Flight Bundle"
    assert detail.msrp_total == 1597
    assert [i.machine_name for i in detail.items] == ["a_x", "b_x", "c_x"]
    # Tier 3 unlocks nothing new, so it's dropped, like the legacy parser does.
    assert [(t.price_amount, [i.name for i in t.items]) for t in detail.tiers] == [(5, ["Book A", "Book B"]), (18, ["Book C"])]
    assert detail.tiers[0].label == "Pay $5 or more to unlock"
    assert detail.tiers[1].label == "Pay $18 or more to also unlock!"


@pytest.mark.asyncio
async def test_detail_flight_item_fields():
    game = _item(
        "g_x", "Game", bundleType="games", msrp={"amount": 24.99, "currencyCode": "USD"},
        platforms=["windows"], deliveryMethods=["steam"], description="Plain text <b>bold</b><script>x</script>",
    )
    html = _flight_html(_flight_page_chunk(_bundle_page([{"id": "1", "price": {"amount": 10}, "bundleItems": [game, _item("noname", "")]}])))
    with _mock_get(html):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/games/x", force=True)

    (item,) = detail.items  # the untitled entry is dropped
    assert item.msrp_amount == 24.99
    assert item.content_type == "games"
    assert (item.authors, item.publishers) == (["Dev One"], ["Pub One"])
    assert (item.formats, item.delivery_methods) == (["Windows"], ["Steam"])
    assert "<script" not in item.description and "bold" in item.description
    assert item.image_url == "https://hb-prd.imgix.net/g_x.png"


@pytest.mark.asyncio
async def test_detail_unresolved_flight_references_are_not_shown_as_descriptions_or_prices():
    html = _flight_html(_flight_page_chunk(_bundle_page([{"id": "1", "price": {"amount": 5}, "bundleItems": [_item("a_x", "Book A")]}])))
    with _mock_get(html):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x", force=True)
    assert detail.items[0].description == ""  # "$70" is a row reference, not text
    assert detail.items[0].msrp_amount is None  # "$undefined"


@pytest.mark.asyncio
async def test_detail_failure_without_any_known_data_raises():
    with _mock_get(_flight_html('1:"$Sreact.fragment"')):
        with pytest.raises(ValueError, match="Next.js bundle data"):
            await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x", force=True)


@pytest.mark.asyncio
async def test_detail_item_machine_names_drop_the_bundle_suffix_so_they_match_owned_orders():
    # Flight data keys items "<item>_<bundle key>"; orders store the bare item
    # machine_name, which is what the catalog (and every owned check) compares.
    tier = {
        "id": "1",
        "sku": "datascienceoreilly_bookbundle_initial",
        "price": {"amount": 5},
        "bundleItems": [_item("datasciencefromscratch_datascienceoreilly_bookbundle", "Data Science from Scratch")],
    }
    with _mock_get(_flight_html(_flight_page_chunk(_bundle_page([tier])))):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x", force=True)
    assert [i.machine_name for i in detail.items] == ["datasciencefromscratch"]


def test_bundle_key_comes_from_the_tier_skus():
    page = {"tiers": [{"sku": "flat2vrbundle_bundle_initial"}, {"sku": "flat2vrbundle_bundle_bt16"}]}
    assert storefront._bundle_key(page) == "flat2vrbundle_bundle"
    assert storefront._bundle_key({"tiers": [{}]}) == ""


# ---------------------------------------------- long-text rows and start date


def test_text_rows_are_read_by_utf8_byte_length_with_no_separator_after_them():
    text = "5:[1]\n70:T" + format(len("Café ☕".encode()), "x") + ",Café ☕71:T3,abc\n6:[2]"
    assert storefront._text_rows(text) == {"70": "Café ☕", "71": "abc"}


def test_text_rows_ignores_a_stream_with_none():
    assert storefront._text_rows('e:[["$","a",null,{}]]\n') == {}


@pytest.mark.asyncio
async def test_a_game_description_sent_as_a_text_row_is_resolved():
    page = _bundle_page([{"id": "1", "sku": "gamebundle_bundle_initial", "price": {"amount": 5}, "bundleItems": [_item("game_gamebundle_bundle", "Game", description="$70")]}])
    page["bundlePage"]["startDate"] = "2026-10-26T18:00:00.000Z"
    body = "<p>It's a <b>great</b> game — really.</p><script>x</script>"
    html = _flight_html(_flight_page_chunk(page) + f"\n70:T{len(body.encode()):x},{body}")
    with _mock_get(html):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/games/x", force=True)

    item = detail.items[0]
    assert "great" in item.description and "<script>" not in item.description
    assert detail.start_date == datetime(2026, 10, 26, 18, 0)


@pytest.mark.asyncio
async def test_an_unresolvable_description_reference_leaves_it_empty():
    page = _bundle_page([{"id": "1", "sku": "b_bundle_initial", "price": {"amount": 5}, "bundleItems": [_item("i_b_bundle", "Item", description="$99")]}])
    with _mock_get(_flight_html(_flight_page_chunk(page))):
        detail = await storefront.fetch_bundle_detail("https://www.humblebundle.com/books/x", force=True)
    assert detail.items[0].description == "" and detail.start_date is None
