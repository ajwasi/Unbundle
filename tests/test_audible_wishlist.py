from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.connectors import audible_connector as ac
from app.models.audible_book import AudibleBook
from app.models.audible_wishlist import AudibleWishlistItem, AudibleWishlistPrice
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import audible_sync


def _raw(asin="B001", title="A Title", price=6.99, list_price=30.23):
    item = {
        "asin": asin,
        "title": title,
        "subtitle": "Book 1",
        "authors": [{"name": "N. K. Jemisin"}],
        "narrators": [{"name": "Robin Miles"}],
        "product_images": {"500": "https://m.media-amazon.com/images/" + asin + ".jpg"},
        "runtime_length_min": 927,
        "date_added": "2026-09-01T12:00:00Z",
    }
    price_obj = {}
    if price is not None:
        price_obj["lowest_price"] = {"base": price, "currency_code": "USD"}
    if list_price is not None:
        price_obj["list_price"] = {"base": list_price, "currency_code": "USD"}
    if price_obj:
        item["price"] = price_obj
    return item


# ------------------------------------------------------------------ parsing


def test_parses_the_documented_wishlist_shape():
    parsed = ac.parse_wishlist_item(_raw())

    assert parsed.asin == "B001"
    assert parsed.title == "A Title"
    assert parsed.authors == "N. K. Jemisin"
    assert parsed.narrators == "Robin Miles"
    assert parsed.current_price == 6.99
    assert parsed.list_price == 30.23
    assert parsed.currency == "USD"
    assert parsed.runtime_minutes == 927
    assert parsed.added_at == datetime(2026, 9, 1, 12, 0)


def test_an_item_with_no_price_still_parses():
    # The price response group is not guaranteed to populate both amounts.
    # Such an item still belongs on the page; it just shows no discount.
    parsed = ac.parse_wishlist_item(_raw(price=None, list_price=None))
    assert parsed.current_price is None
    assert parsed.list_price is None


def test_a_flat_price_shape_is_still_read():
    item = _raw(price=None, list_price=None)
    item["price"] = {"base": 9.99, "currency_code": "GBP"}

    parsed = ac.parse_wishlist_item(item)
    assert parsed.current_price == 9.99
    assert parsed.currency == "GBP"


def test_an_item_without_an_asin_is_dropped():
    assert ac.parse_wishlist_item({"title": "No identity"}) is None


# ----------------------------------------------------------------- discount


@pytest.mark.parametrize(
    "price,list_price,expected",
    [
        (6.99, 30.23, 77),
        (10.0, 20.0, 50),
        (20.0, 20.0, None),  # same price is not a discount
        (25.0, 20.0, None),  # data disagreeing with itself is not a discount
        (None, 20.0, None),
        (5.0, None, None),
    ],
)
def test_discount_only_when_both_prices_agree_there_is_one(price, list_price, expected):
    item = AudibleWishlistItem(asin="X", current_price=price, list_price=list_price)
    assert item.discount_pct == expected


# --------------------------------------------------------------------- sync


def _connect(db):
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK, encrypted_payload=encrypt_json({"locale_code": "us"})))
    db.commit()


class _Auth:
    @classmethod
    def from_dict(cls, data):
        return cls()

    def to_dict(self):
        return {"locale_code": "us"}


async def _run_sync(db, entries):
    with patch("audible.Authenticator", _Auth), patch.object(
        ac, "fetch_wishlist", new=AsyncMock(return_value=entries)
    ):
        return await audible_sync.refresh_audible_wishlist(db)


@pytest.mark.asyncio
async def test_sync_inserts_items_and_one_price_row_each(db):
    _connect(db)
    entries = [ac.parse_wishlist_item(_raw("B001")), ac.parse_wishlist_item(_raw("B002", price=1.99))]

    result = await _run_sync(db, entries)

    assert result["total"] == 2
    assert result["new"] == 2
    assert result["price_changes"] == 2
    assert db.query(AudibleWishlistPrice).count() == 2


@pytest.mark.asyncio
async def test_an_unchanged_price_records_no_new_row(db):
    _connect(db)
    entries = [ac.parse_wishlist_item(_raw("B001"))]

    await _run_sync(db, entries)
    result = await _run_sync(db, entries)

    assert result["price_changes"] == 0
    assert db.query(AudibleWishlistPrice).count() == 1


@pytest.mark.asyncio
async def test_a_price_drop_appends_a_row(db):
    _connect(db)
    await _run_sync(db, [ac.parse_wishlist_item(_raw("B001", price=30.23))])

    result = await _run_sync(db, [ac.parse_wishlist_item(_raw("B001", price=6.99))])

    assert result["price_changes"] == 1
    assert db.query(AudibleWishlistPrice).count() == 2
    assert db.get(AudibleWishlistItem, "B001").current_price == 6.99


@pytest.mark.asyncio
async def test_a_removed_item_is_flagged_and_keeps_its_history(db):
    _connect(db)
    await _run_sync(db, [ac.parse_wishlist_item(_raw("B001")), ac.parse_wishlist_item(_raw("B002"))])

    result = await _run_sync(db, [ac.parse_wishlist_item(_raw("B001"))])

    assert result["removed"] == 1
    assert db.query(AudibleWishlistItem).count() == 2  # flagged, never deleted
    assert db.get(AudibleWishlistItem, "B002").removed_at is not None
    assert db.query(AudibleWishlistPrice).filter(AudibleWishlistPrice.asin == "B002").count() == 1


@pytest.mark.asyncio
async def test_sync_without_a_credential_raises_not_connected(db):
    with pytest.raises(audible_sync.NotConnectedError):
        await audible_sync.refresh_audible_wishlist(db)


# --------------------------------------------------------------------- page


def _seed(db, asin="B001", title="The Fifth Season", price=6.99, list_price=30.23, low=6.99):
    now = datetime.utcnow()
    db.add(
        AudibleWishlistItem(
            asin=asin,
            title=title,
            authors="N. K. Jemisin",
            current_price=price,
            list_price=list_price,
            currency="USD",
            added_at=now,
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    db.add(AudibleWishlistPrice(asin=asin, price=low, currency="USD", captured_at=now))
    db.commit()


def test_page_requires_auth(client):
    assert client.get("/wishlist", follow_redirects=False).status_code == 303


def test_empty_state_points_at_the_sync_button(authed_client):
    assert "Nothing synced yet" in authed_client.get("/wishlist").text


def test_page_shows_price_discount_and_lowest_seen(authed_client, db):
    _seed(db)
    resp = authed_client.get("/wishlist")

    assert "The Fifth Season" in resp.text
    assert "-77%" in resp.text
    assert "Lowest yet" in resp.text


def test_owned_wishlist_items_are_flagged(authed_client, db):
    _seed(db)
    db.add(AudibleBook(asin="B001", title="The Fifth Season"))
    db.commit()

    assert "Owned" in authed_client.get("/wishlist").text


def test_deals_only_filter_hides_undiscounted_items(authed_client, db):
    _seed(db, asin="B001", title="Discounted One")
    _seed(db, asin="B002", title="Full Price One", price=20.0, list_price=20.0, low=20.0)

    resp = authed_client.get("/wishlist", params={"deals_only": "true"})
    assert "Discounted One" in resp.text
    assert "Full Price One" not in resp.text


def test_search_matches_title_and_author(authed_client, db):
    _seed(db)
    assert "Fifth Season" in authed_client.get("/wishlist", params={"q": "jemisin"}).text
    assert "Fifth Season" not in authed_client.get("/wishlist", params={"q": "tolkien"}).text


def test_refresh_without_audible_explains_rather_than_500s(authed_client):
    resp = authed_client.post("/wishlist/refresh")

    assert resp.status_code == 200
    assert "not connected yet" in resp.text


def test_sidebar_links_to_the_page(authed_client):
    assert 'href="/wishlist"' in authed_client.get("/downloads").text
