from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.connectors import audible_connector
from app.connectors.audible_connector import AudibleBookData
from app.models.audible_book import AudibleBook
from app.models.audible_series_book import AudibleSeriesBook
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import audible_sync

# Real captures (2026-10-06) against catalog/products/{asin} with
# response_groups=relationships,series,price,product_desc,contributors,media,rating —
# see audible_connector.py's PROBE_RESPONSE_GROUPS and probe_catalog_product().
SERIES_PRODUCT = {
    "product": {
        "asin": "B01M1RDL6W",
        "title": "Bobiverse",
        "relationships": [
            {"asin": "B088C4DBYP", "relationship_to_product": "child", "relationship_type": "series", "sequence": "4", "sort": "4"},
            {"asin": "B01N17THEO", "relationship_to_product": "child", "relationship_type": "series", "sequence": "2", "sort": "2"},
            {"asin": "B01L082HJ2", "relationship_to_product": "child", "relationship_type": "series", "sequence": "1", "sort": "1"},
            {"asin": "B07341FZDC", "relationship_to_product": "child", "relationship_type": "series", "sequence": "3", "sort": "3"},
        ],
    }
}

BOOK_PRODUCT = {
    "product": {
        "asin": "B01L082HJ2",
        "title": "We Are Legion (We Are Bob)",
        "authors": [{"asin": "B010ETTBJC", "name": "Dennis E. Taylor"}],
        "narrators": [{"name": "Ray Porter"}],
        "product_images": {"500": "https://m.media-amazon.com/images/I/51yMirAE25L._SL500_.jpg"},
        "price": {
            "credit_price": 1.0,
            "list_price": {"base": 19.950000762939453, "currency_code": "USD", "type": "list"},
            "lowest_price": {"base": 13.960000038146973, "currency_code": "USD", "type": "member"},
        },
        "series": [{"asin": "B01M1RDL6W", "sequence": "1", "title": "Bobiverse", "url": "/pd/Bobiverse-Audiobook/B01M1RDL6W"}],
    }
}


# ----------------------------------------------------------------- connector


def test_parse_series_reads_the_series_own_asin_too():
    assert audible_connector._parse_series(BOOK_PRODUCT["product"]) == ("Bobiverse", "1", "B01M1RDL6W")


def test_parse_series_degrades_when_there_is_no_series():
    assert audible_connector._parse_series({"title": "Standalone"}) == ("", "", "")


async def test_fetch_series_children_reads_series_relationships_only():
    with patch.object(audible_connector, "probe_catalog_product", new=AsyncMock(return_value=SERIES_PRODUCT)):
        children = await audible_connector.fetch_series_children(auth=None, series_asin="B01M1RDL6W")

    assert set(children) == {("B088C4DBYP", "4"), ("B01N17THEO", "2"), ("B01L082HJ2", "1"), ("B07341FZDC", "3")}


def test_parse_series_book_item_reuses_the_wishlist_price_shape():
    parsed = audible_connector.parse_series_book_item(BOOK_PRODUCT["product"], sequence="1")

    assert parsed.asin == "B01L082HJ2"
    assert parsed.title == "We Are Legion (We Are Bob)"
    assert parsed.authors == "Dennis E. Taylor"
    assert parsed.narrators == "Ray Porter"
    assert parsed.current_price == pytest.approx(13.96)
    assert parsed.list_price == pytest.approx(19.95)
    assert parsed.currency == "USD"
    assert parsed.sequence == "1"


def test_parse_series_book_item_without_an_asin_is_dropped():
    assert audible_connector.parse_series_book_item({"title": "No asin"}, sequence="1") is None


# ------------------------------------------------------------------------- sync


class _FakeAuth:
    def to_dict(self):
        return {"access_token": "AT", "locale_code": "us"}


def _connect_audible(db):
    db.add(Credential(source=SOURCE_AUDIBLE, encrypted_payload=encrypt_json({"access_token": "AT", "locale_code": "us"})))
    db.commit()


async def test_refresh_stores_unowned_series_siblings(db):
    _connect_audible(db)
    books = [
        AudibleBookData(
            asin="B01L082HJ2", title="We Are Legion (We Are Bob)", author="Dennis E. Taylor", runtime_minutes=596,
            cover_url="", series_title="Bobiverse", series_sequence="1", series_asin="B01M1RDL6W",
        )
    ]

    async def fake_probe(auth, asin):
        if asin == "B01M1RDL6W":
            return SERIES_PRODUCT
        product = dict(BOOK_PRODUCT["product"], asin=asin, title=f"Book {asin}")
        return {"product": product}

    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)),
        patch.object(audible_connector, "probe_catalog_product", new=fake_probe),
    ):
        await audible_sync.refresh_audible_library(db)

    # B01L082HJ2 is already owned (it's the representative book itself), so
    # only its three still-unowned siblings should be stored.
    rows = db.query(AudibleSeriesBook).all()
    assert {r.asin for r in rows} == {"B088C4DBYP", "B01N17THEO", "B07341FZDC"}
    assert all(r.series_asin == "B01M1RDL6W" for r in rows)
    assert all(r.series_title == "Bobiverse" for r in rows)


async def test_a_series_that_fails_to_load_does_not_fail_the_refresh(db):
    _connect_audible(db)
    books = [
        AudibleBookData(
            asin="B01L082HJ2", title="We Are Legion (We Are Bob)", author="Dennis E. Taylor", runtime_minutes=596,
            cover_url="", series_title="Bobiverse", series_sequence="1", series_asin="B01M1RDL6W",
        )
    ]

    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)),
        patch.object(audible_connector, "fetch_series_children", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        count = await audible_sync.refresh_audible_library(db)

    assert count == 1
    assert db.get(AudibleBook, "B01L082HJ2") is not None
    assert db.query(AudibleSeriesBook).count() == 0


async def test_a_book_with_no_series_is_skipped(db):
    _connect_audible(db)
    books = [AudibleBookData(asin="B999", title="Standalone", author="Someone", runtime_minutes=300, cover_url="")]

    with (
        patch("app.sync.audible_sync.audible.Authenticator.from_dict", return_value=_FakeAuth()),
        patch("app.sync.audible_sync.audible_connector.fetch_library", new=AsyncMock(return_value=books)),
    ):
        await audible_sync.refresh_audible_library(db)

    assert db.query(AudibleSeriesBook).count() == 0


# ----------------------------------------------------------------------- router


def test_missing_series_section_lists_unowned_siblings(authed_client, db):
    now = datetime.utcnow()
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK, encrypted_payload=encrypt_json({})))
    db.add(AudibleBook(asin="B01L082HJ2", title="We Are Legion (We Are Bob)", author="Dennis E. Taylor", runtime_minutes=596, cover_url="", fetched_at=now, series_asin="B01M1RDL6W"))
    db.add(AudibleSeriesBook(series_asin="B01M1RDL6W", asin="B01N17THEO", series_title="Bobiverse", title="For We Are Many", authors="Dennis E. Taylor", sequence="2", current_price=13.96, list_price=19.95, currency="USD", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible")
    assert "Missing from your series" in resp.text
    assert "For We Are Many" in resp.text


def test_a_book_bought_since_the_last_series_refresh_drops_off_the_list(authed_client, db):
    now = datetime.utcnow()
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK, encrypted_payload=encrypt_json({})))
    db.add(AudibleBook(asin="B01L082HJ2", title="We Are Legion (We Are Bob)", author="Dennis E. Taylor", runtime_minutes=596, cover_url="", fetched_at=now, series_asin="B01M1RDL6W"))
    # Now owned too, but the stale series row hasn't been cleared by a refresh yet.
    db.add(AudibleBook(asin="B01N17THEO", title="For We Are Many", author="Dennis E. Taylor", runtime_minutes=600, cover_url="", fetched_at=now, series_asin="B01M1RDL6W"))
    db.add(AudibleSeriesBook(series_asin="B01M1RDL6W", asin="B01N17THEO", series_title="Bobiverse", title="For We Are Many", authors="Dennis E. Taylor", sequence="2", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible")
    assert "Missing from your series" not in resp.text


def test_series_detail_page_lists_owned_and_missing_books_in_order(authed_client, db):
    now = datetime.utcnow()
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK, encrypted_payload=encrypt_json({})))
    db.add(AudibleBook(asin="B01L082HJ2", title="We Are Legion (We Are Bob)", author="Dennis E. Taylor", runtime_minutes=596,
                       cover_url="https://example.com/bob1.jpg", price_amount=13.96, price_currency="USD", fetched_at=now,
                       series_title="Bobiverse", series_sequence="1", series_asin="B01M1RDL6W"))
    db.add(AudibleSeriesBook(series_asin="B01M1RDL6W", asin="B01N17THEO", series_title="Bobiverse", title="For We Are Many",
                             authors="Dennis E. Taylor", sequence="2", cover_url="https://example.com/bob2.jpg",
                             current_price=13.96, list_price=19.95, currency="USD", fetched_at=now))
    db.commit()

    resp = authed_client.get("/audible/series/B01M1RDL6W")
    assert resp.status_code == 200
    assert "Bobiverse" in resp.text
    assert resp.text.index("We Are Legion") < resp.text.index("For We Are Many")
    assert resp.text.count(">Owned<") == 1
    assert resp.text.count(">Missing<") == 1
    assert 'href="/audible/B01L082HJ2"' in resp.text
    assert 'href="https://www.audible.com/pd/B01N17THEO"' in resp.text
    # Owned row shows what was paid; missing row shows the live price. Both covers render.
    assert resp.text.count("13.96") == 2
    assert 'src="https://example.com/bob1.jpg"' in resp.text
    assert 'src="https://example.com/bob2.jpg"' in resp.text


def test_series_detail_page_404s_for_an_unknown_series(authed_client):
    resp = authed_client.get("/audible/series/NOSUCHSERIES")
    assert resp.status_code == 404


def test_list_page_links_series_name_to_its_detail_page(authed_client, db):
    now = datetime.utcnow()
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK, encrypted_payload=encrypt_json({})))
    db.add(AudibleBook(asin="B01L082HJ2", title="We Are Legion (We Are Bob)", author="Dennis E. Taylor", runtime_minutes=596, cover_url="", fetched_at=now, series_title="Bobiverse", series_sequence="1", series_asin="B01M1RDL6W"))
    db.commit()

    resp = authed_client.get("/audible")
    assert 'href="/audible/series/B01M1RDL6W"' in resp.text
