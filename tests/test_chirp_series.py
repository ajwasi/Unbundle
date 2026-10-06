import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from app.connectors import chirp_connector as chirp
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.chirp_series_book import ChirpSeriesBook
from app.models.credential import SOURCE_CHIRP, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import chirp_sync

FIXTURES = Path(__file__).parent / "fixtures"
SERIES_URL = "/series/the-silo-saga-audiobooks"
WOOL_PATH = "/audiobooks/wool-by-hugh-howey-4415b4a1b5"


def _series_html() -> str:
    return (FIXTURES / "chirp_series_page.html").read_text(encoding="utf-8")


def _book_page_html() -> str:
    data = {"displayTitle": "Wool", "seriesName": "The Silo Saga", "seriesNumber": "#1", "seriesUrl": SERIES_URL}
    attr = json.dumps(data).replace('"', "&quot;")
    return f'<div data-audiobook="{attr}" id="audiobook-app"></div>'


def test_parse_series_books_reads_each_card():
    books = chirp.parse_series_books(_series_html())

    assert [b.title for b in books] == ["Wool", "Shift"]
    wool, shift = books
    assert wool.url_path == WOOL_PATH
    assert wool.series_number == "1"
    assert wool.authors == "Hugh Howey"
    assert wool.listing_price == 22.95
    assert wool.current_price == 14.99
    assert shift.series_number == "2"
    assert shift.current_price == 20.99


def test_a_card_with_no_discount_has_only_its_current_price():
    html = _series_html().replace('<div class="display_pricing-module__discountPrice___w9zsG" aria-label="Discount price: $20.99">$20.99</div>', "")
    shift = chirp.parse_series_books(html)[1]
    assert shift.current_price == 22.95
    assert shift.listing_price is None


def test_parse_series_url_reads_the_series_a_book_belongs_to():
    assert chirp.parse_series_url(_book_page_html()) == SERIES_URL


def test_a_book_page_without_a_series_has_no_series_url():
    assert chirp.parse_series_url('<div data-audiobook="{&quot;displayTitle&quot;:&quot;Standalone&quot;}"></div>') is None
    assert chirp.parse_series_url("<html>no json here</html>") is None


def _library_payload() -> dict:
    return {
        "data": {
            "currentUserAudiobooks": [
                {
                    "id": "27991647",
                    "progressStatus": "IN_PROGRESS",
                    "positionPercent": 14,
                    "playable": True,
                    "audiobook": {
                        "id": "626000",
                        "url": WOOL_PATH,
                        "coverUrl": "",
                        "displayTitle": "Wool",
                        "displayAuthors": "Hugh Howey",
                        "displayNarrators": "Edoardo Ballerini",
                        "seriesAudiobook": {"displayNumber": "1", "series": {"name": "The Silo Saga"}},
                        "currentProduct": None,
                    },
                }
            ],
            "currentUserAudiobooksCount": 1,
        }
    }


async def test_refresh_stores_every_book_in_each_owned_series(db):
    db.add(Credential(source=SOURCE_CHIRP, encrypted_payload=encrypt_json({"cookie": "cf_clearance=abc"})))
    db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/purchases":
            return httpx.Response(200, text='<div class="purchases-list"></div>')
        if request.url.path == WOOL_PATH:
            return httpx.Response(200, text=_book_page_html())
        if request.url.path == SERIES_URL:
            return httpx.Response(200, text=_series_html())
        return httpx.Response(200, json=_library_payload())

    def build(cookie_header: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with patch.object(chirp, "client_from_cookie_header", new=build):
        await chirp_sync.refresh_chirp_library(db)

    rows = db.query(ChirpSeriesBook).order_by(ChirpSeriesBook.series_number).all()
    assert [r.title for r in rows] == ["Wool", "Shift"]
    assert all(r.series_name == "The Silo Saga" for r in rows)


async def test_a_series_that_fails_to_load_does_not_fail_the_refresh(db):
    db.add(Credential(source=SOURCE_CHIRP, encrypted_payload=encrypt_json({"cookie": "cf_clearance=abc"})))
    db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/purchases":
            return httpx.Response(200, text='<div class="purchases-list"></div>')
        if request.url.path == WOOL_PATH:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_library_payload())

    def build(cookie_header: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with patch.object(chirp, "client_from_cookie_header", new=build):
        count = await chirp_sync.refresh_chirp_library(db)

    assert count == 1
    assert db.query(ChirpSeriesBook).count() == 0


def test_the_page_lists_series_books_you_do_not_own(authed_client, db):
    now = datetime.utcnow()
    db.add(Credential(source=SOURCE_CHIRP, status=STATUS_OK, encrypted_payload=encrypt_json({"cookie": "cf_clearance=abc"})))
    db.add(ChirpAudiobook(purchase_id="1", url_path=WOOL_PATH, title="Wool", fetched_at=now))
    db.add(ChirpSeriesBook(series_url=SERIES_URL, url_path=WOOL_PATH, series_name="The Silo Saga", title="Wool",
                           authors="Hugh Howey", series_number="1", current_price=14.99, fetched_at=now))
    db.add(ChirpSeriesBook(series_url=SERIES_URL, url_path="/audiobooks/shift-by-hugh-howey-ff77656a0a",
                           series_name="The Silo Saga", title="Shift", authors="Hugh Howey", series_number="2",
                           listing_price=22.95, current_price=20.99, fetched_at=now))
    db.commit()

    resp = authed_client.get("/chirp")
    assert "Missing from your series" in resp.text
    missing = resp.text.split("Missing from your series", 1)[1]
    assert "Shift" in missing
    assert "20.99" in missing
    assert "-9%" in missing
    assert "Wool" not in missing
