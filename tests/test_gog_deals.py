from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors import gog_deals
from app.models.gog_game import GogGame


def _product(pid="1211776926", title="Dead Age", discount="-95%", final="1.49", base="29.59", rating=44):
    return {
        "id": pid,
        "title": title,
        "slug": "dead_age",
        "storeLink": "https://www.gog.com/en/game/dead_age",
        "coverVertical": "https://images.gog-statics.com/abc.png",
        "price": {
            "final": f"${final}",
            "base": f"${base}",
            "discount": discount,
            "finalMoney": {"amount": final, "currency": "USD"},
            "baseMoney": {"amount": base, "currency": "USD"},
        },
        "operatingSystems": ["windows", "linux"],
        "developers": ["Silent Dreams"],
        "genres": [{"name": "Role-playing"}, {"name": "Survival"}, {"name": "Turn-based"}, {"name": "Extra"}],
        "reviewsRating": rating,
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    gog_deals._cache = None
    yield
    gog_deals._cache = None


# ------------------------------------------------------------------ parsing


def test_parses_the_live_product_shape():
    deal = gog_deals.parse_deal(_product())

    assert deal.product_id == 1211776926  # int, to join GogGame.product_id
    assert deal.title == "Dead Age"
    assert deal.price_final == 1.49
    assert deal.price_base == 29.59
    assert deal.currency == "USD"
    assert deal.discount_pct == 95
    assert deal.operating_systems == ["windows", "linux"]
    assert deal.developers == ["Silent Dreams"]
    assert deal.genres == ["Role-playing", "Survival", "Turn-based"]  # capped at three


def test_a_product_without_a_usable_id_is_dropped():
    # No identity means it can neither be matched against the library nor
    # linked to, so it has no place on the page.
    assert gog_deals.parse_deal({"title": "No id"}) is None
    assert gog_deals.parse_deal({"id": "not-a-number", "title": "Bad id"}) is None


def test_discount_is_read_from_gogs_own_label_not_recomputed():
    # The number shown should be the one GOG advertises, even if it disagrees
    # slightly with the two amounts.
    assert gog_deals.parse_deal(_product(discount="-33%")).discount_pct == 33
    assert gog_deals.parse_deal(_product(discount=None)).discount_pct is None


@pytest.mark.parametrize("raw,expected", [(49, 4.9), (44, 4.4), (5, 0.5), (0, None), (None, None), ("x", None)])
def test_rating_is_normalised_out_of_five_and_zero_means_unrated(raw, expected):
    assert gog_deals.parse_deal(_product(rating=raw)).rating == expected


# ----------------------------------------------------------------- fetching


def _response(products, total=4647):
    return httpx.Response(
        200,
        json={"products": products, "productCount": total},
        request=httpx.Request("GET", gog_deals.CATALOG_URL),
    )


@pytest.mark.asyncio
async def test_fetch_stops_on_a_short_page():
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_response([_product()]))) as mock:
        deals, total = await gog_deals.fetch_deals()

    assert len(deals) == 1
    assert total == 4647
    assert mock.await_count == 1  # short page, so no second request


@pytest.mark.asyncio
async def test_results_are_cached_until_forced():
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_response([_product()]))) as mock:
        await gog_deals.fetch_deals()
        await gog_deals.fetch_deals()
        assert mock.await_count == 1

        await gog_deals.fetch_deals(force=True)
        assert mock.await_count == 2


# --------------------------------------------------------------------- page


def _patch_fetch(deals, total=4647):
    return patch.object(gog_deals, "fetch_deals", new=AsyncMock(return_value=(deals, total)))


def _deal(pid, title="A Game", discount=90, price=1.99, rating=4.4):
    return gog_deals.GogDeal(
        product_id=pid,
        title=title,
        slug="x",
        store_url="https://www.gog.com/en/game/x",
        cover_url="https://images.gog-statics.com/x.png",
        price_final=price,
        price_base=19.99,
        currency="USD",
        discount_pct=discount,
        operating_systems=["windows"],
        developers=["A Dev"],
        genres=["Action"],
        rating=rating,
    )


def test_page_requires_auth(client):
    assert client.get("/deals", follow_redirects=False).status_code == 303


def test_page_lists_deals(authed_client):
    with _patch_fetch([_deal(1, "Dead Age")]):
        resp = authed_client.get("/deals")

    assert "Dead Age" in resp.text
    assert "-90%" in resp.text
    assert "1.99" in resp.text


def test_owned_titles_are_matched_by_product_id(authed_client, db):
    db.add(GogGame(product_id=1, title="Owned Game"))
    db.commit()

    with _patch_fetch([_deal(1, "Owned Game"), _deal(2, "Unowned Game")]):
        resp = authed_client.get("/deals")

    assert ">Owned<" in resp.text
    assert "New to you" in resp.text
    assert "you own 1 of them" in resp.text


def test_hide_owned_filter(authed_client, db):
    db.add(GogGame(product_id=1, title="Owned Game"))
    db.commit()

    with _patch_fetch([_deal(1, "Owned Game"), _deal(2, "Unowned Game")]):
        resp = authed_client.get("/deals", params={"hide_owned": "true"})

    assert "Owned Game" not in resp.text
    assert "Unowned Game" in resp.text


def test_minimum_discount_filter(authed_client):
    with _patch_fetch([_deal(1, "Deep Cut", discount=95), _deal(2, "Shallow Cut", discount=20)]):
        resp = authed_client.get("/deals", params={"min_discount": 75})

    assert "Deep Cut" in resp.text
    assert "Shallow Cut" not in resp.text


def test_search_matches_title_and_developer(authed_client):
    with _patch_fetch([_deal(1, "Findable")]):
        assert "Findable" in authed_client.get("/deals", params={"q": "find"}).text
        assert "Findable" in authed_client.get("/deals", params={"q": "a dev"}).text
        assert "Findable" not in authed_client.get("/deals", params={"q": "nothing"}).text


def test_sorting_by_price_puts_the_cheapest_first(authed_client):
    with _patch_fetch([_deal(1, "Pricey", price=20.0), _deal(2, "Cheap", price=1.0)]):
        resp = authed_client.get("/deals", params={"sort": "price"})

    assert resp.text.index("Cheap") < resp.text.index("Pricey")


def test_without_gog_connected_the_page_still_works(authed_client):
    with _patch_fetch([_deal(1, "A Game")]):
        resp = authed_client.get("/deals")

    assert "A Game" in resp.text
    assert "Connect <strong>GOG</strong>" in resp.text


def test_a_gog_outage_reads_as_a_source_problem_not_a_broken_page(authed_client):
    with patch.object(gog_deals, "fetch_deals", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = authed_client.get("/deals")

    assert resp.status_code == 200
    assert "Could not reach GOG" in resp.text


def test_htmx_request_returns_only_the_table(authed_client):
    with _patch_fetch([_deal(1)]):
        resp = authed_client.get("/deals", headers={"HX-Request": "true"})

    assert "<html" not in resp.text
    assert 'id="deals-table"' in resp.text


def test_sidebar_links_to_the_page(authed_client):
    assert 'href="/deals"' in authed_client.get("/downloads").text
