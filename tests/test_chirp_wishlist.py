from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.connectors import chirp_connector as chirp
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.credential import SOURCE_CHIRP, Credential
from app.models.store_wishlist import ChirpWishlistItem, ChirpWishlistPrice
from app.security import encrypt_json
from app.sync import store_wishlist_sync as sync

FIXTURE = Path(__file__).parent / "fixtures" / "chirp_wishlist_page.html"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _connect(db, cookie="cf_clearance=abc"):
    db.add(Credential(source=SOURCE_CHIRP, encrypted_payload=encrypt_json({"cookie": cookie})))
    db.commit()


def _cookie_client_stub(handler):
    def build(cookie_header: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Cookie": cookie_header}, follow_redirects=True)

    return build


def test_parse_wishlist_page_reads_the_grid_and_skips_the_carousel():
    entries = chirp.parse_wishlist_page(_fixture_html())

    assert [e.title for e in entries] == ["Good Strategy Bad Strategy", "Rights of Man"]


def test_wishlist_entry_carries_author_cover_and_prices():
    good = chirp.parse_wishlist_page(_fixture_html())[0]
    assert good.url_path == "/audiobooks/good-strategy-bad-strategy-by-richard-rumelt"
    assert good.authors == "Richard Rumelt"
    assert good.cover_url.startswith("https://img.chirpbooks.com/")
    assert good.current_price == 20.99
    assert good.list_price == 22.5


def test_a_discount_only_wishlist_entry_has_no_list_price():
    rights = chirp.parse_wishlist_page(_fixture_html())[1]
    assert rights.current_price == 7.99
    assert rights.list_price is None


async def test_refresh_wishlist_stores_items_and_records_price_history(db):
    _connect(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture_html())

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        result = await sync.refresh_chirp_wishlist(db)

    assert result["total"] == 2
    assert result["new"] == 2
    assert db.query(ChirpWishlistItem).count() == 2
    assert db.query(ChirpWishlistPrice).count() == 2


async def test_an_item_that_disappears_from_the_wishlist_is_flagged_not_deleted(db):
    _connect(db)
    now = datetime.utcnow()
    db.add(ChirpWishlistItem(url_path="/audiobooks/gone", title="Gone", first_seen_at=now, last_seen_at=now))
    db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture_html())

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        result = await sync.refresh_chirp_wishlist(db)

    assert result["removed"] == 1
    assert db.get(ChirpWishlistItem, "/audiobooks/gone").removed_at is not None


async def test_refresh_without_a_connection_is_a_not_connected_error(db):
    with pytest.raises(sync.NotConnectedError):
        await sync.refresh_chirp_wishlist(db)


def test_chirp_wishlist_page_shows_its_books_with_no_rating_column(authed_client, db):
    now = datetime.utcnow()
    db.add(
        ChirpWishlistItem(
            url_path="/audiobooks/rights-of-man", title="Rights of Man", authors="Thomas Paine",
            current_price=7.99, first_seen_at=now, last_seen_at=now,
        )
    )
    db.commit()

    resp = authed_client.get("/wishlist/chirp")
    assert "Rights of Man" in resp.text
    assert "Thomas Paine" in resp.text
    assert "Metacritic" not in resp.text
    assert ">Rating<" not in resp.text


def test_owned_chirp_books_are_matched_by_url_path(authed_client, db):
    now = datetime.utcnow()
    db.add(ChirpWishlistItem(url_path="/audiobooks/wool", title="Wool", first_seen_at=now, last_seen_at=now))
    db.add(ChirpWishlistItem(url_path="/audiobooks/dust", title="Dust", first_seen_at=now, last_seen_at=now))
    db.add(ChirpAudiobook(purchase_id="1", url_path="/audiobooks/wool", title="Wool", fetched_at=now))
    db.commit()

    resp = authed_client.get("/wishlist/chirp")
    assert resp.text.count(">Owned<") == 1


def test_sorting_chirp_by_author(authed_client, db):
    now = datetime.utcnow()
    db.add(ChirpWishlistItem(url_path="/audiobooks/z", title="Zed", authors="Zara Author", first_seen_at=now, last_seen_at=now))
    db.add(ChirpWishlistItem(url_path="/audiobooks/a", title="Ay", authors="Abe Author", first_seen_at=now, last_seen_at=now))
    db.commit()

    resp = authed_client.get("/wishlist/chirp?sort=author")
    assert resp.text.index("Abe Author") < resp.text.index("Zara Author")


def test_chirp_appears_as_a_tab(authed_client):
    resp = authed_client.get("/wishlist/chirp")
    assert 'href="/wishlist/chirp"' in resp.text
