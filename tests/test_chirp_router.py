from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.connectors import chirp_connector
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.credential import SOURCE_CHIRP, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import chirp_sync


def _connect(db, cookie="cf_clearance=abc; _mockingjay_session=xyz"):
    db.add(Credential(source=SOURCE_CHIRP, status=STATUS_OK, encrypted_payload=encrypt_json({"cookie": cookie})))
    db.commit()


def _book(purchase_id="1", title="Wool", authors="Hugh Howey", narrators="Edoardo Ballerini", **overrides):
    defaults = dict(
        purchase_id=purchase_id,
        audiobook_id="626000",
        title=title,
        authors=authors,
        narrators=narrators,
        url_path="/audiobooks/wool-by-hugh-howey-4415b4a1b5",
        cover_url="https://img.chirpbooks.com/x.jpg",
        progress_status="IN_PROGRESS",
        position_percent=14,
        playable=True,
        series_name="The Silo Saga",
        series_number="1",
        listing_price=22.95,
        discount_price=14.99,
        fetched_at=datetime.utcnow(),
    )
    defaults.update(overrides)
    return ChirpAudiobook(**defaults)


def test_page_requires_auth(client):
    assert client.get("/chirp", follow_redirects=False).status_code == 303


def test_refresh_route_requires_auth(client):
    assert client.post("/chirp/refresh", follow_redirects=False).status_code == 303


def test_page_points_at_settings_when_not_connected(authed_client):
    resp = authed_client.get("/chirp")
    assert "isn't connected yet" in resp.text or "isn&#39;t connected yet" in resp.text
    assert '<a href="/settings">' in resp.text


def test_page_lists_synced_books(authed_client, db):
    _connect(db)
    db.add(_book())
    db.commit()

    resp = authed_client.get("/chirp")
    assert "Wool" in resp.text
    assert "Hugh Howey" in resp.text
    assert "14.99" in resp.text
    assert "22.95" in resp.text  # struck-through original price still shown


def test_no_books_synced_yet_shows_a_prompt_to_refresh(authed_client, db):
    _connect(db)
    resp = authed_client.get("/chirp")
    assert "click Refresh" in resp.text


def test_search_filters_by_title_author_or_narrator(authed_client, db):
    _connect(db)
    db.add(_book(purchase_id="1", title="Findable Title"))
    db.add(_book(purchase_id="2", title="Other", authors="Findable Author"))
    db.add(_book(purchase_id="3", title="Nope", authors="Nobody", narrators="Findable Narrator"))
    db.add(_book(purchase_id="4", title="Unrelated", authors="Someone Else", narrators="Someone Else"))
    db.commit()

    resp = authed_client.get("/chirp", params={"q": "findable"})
    assert "Findable Title" in resp.text
    assert "Other" in resp.text  # matched via author
    assert "Nope" in resp.text  # matched via narrator
    assert "Unrelated" not in resp.text


def test_sorting_by_price_puts_the_cheapest_first(authed_client, db):
    _connect(db)
    db.add(_book(purchase_id="1", title="Pricey", listing_price=30.0, discount_price=None))
    db.add(_book(purchase_id="2", title="Cheap", listing_price=5.0, discount_price=None))
    db.commit()

    resp = authed_client.get("/chirp", params={"sort": "price", "dir": "asc"})
    assert resp.text.index("Cheap") < resp.text.index("Pricey")


def test_price_sort_uses_the_discount_price_when_present(authed_client, db):
    _connect(db)
    # Listing price alone would rank this last; its discount makes it the cheapest.
    db.add(_book(purchase_id="1", title="Deep Discount", listing_price=50.0, discount_price=1.0))
    db.add(_book(purchase_id="2", title="No Discount", listing_price=10.0, discount_price=None))
    db.commit()

    resp = authed_client.get("/chirp", params={"sort": "price", "dir": "asc"})
    assert resp.text.index("Deep Discount") < resp.text.index("No Discount")


def test_refresh_syncs_and_shows_the_result(authed_client, db):
    _connect(db)

    async def fake_refresh(db_arg):
        db_arg.add(_book())
        db_arg.commit()
        return 1

    with patch.object(chirp_sync, "refresh_chirp_library", new=fake_refresh):
        resp = authed_client.post("/chirp/refresh")

    assert "Wool" in resp.text


def test_refresh_without_a_connection_does_not_crash(authed_client):
    resp = authed_client.post("/chirp/refresh")
    assert resp.status_code == 200


def test_a_broken_refresh_reads_as_a_connector_failure_not_a_500(authed_client, db):
    _connect(db)
    with patch.object(chirp_connector, "fetch_library_page", new=AsyncMock(side_effect=RuntimeError("boom"))):
        resp = authed_client.post("/chirp/refresh")

    assert resp.status_code == 200
    assert "boom" in resp.text


def test_htmx_sort_or_search_returns_only_the_table_not_the_whole_content(authed_client, db):
    # A sort/search swaps just #chirp-books-table — returning the whole
    # #chirp-content here nests a fresh copy of the count line and table
    # inside the old table on every click.
    _connect(db)
    db.add(_book())
    db.commit()

    resp = authed_client.get("/chirp", params={"sort": "author"}, headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert 'id="chirp-books-table"' in resp.text
    assert 'id="chirp-content"' not in resp.text
    assert "audiobook(s) owned" not in resp.text


def test_repeated_sorts_never_duplicate_the_count_line(authed_client, db):
    _connect(db)
    db.add(_book(purchase_id="1", title="A Book"))
    db.add(_book(purchase_id="2", title="B Book"))
    db.commit()

    full = authed_client.get("/chirp")
    assert full.text.count("audiobook(s) owned") == 1

    # Each htmx response replaces #chirp-books-table only, so none of them may carry the count line.
    for params in ({"sort": "author"}, {"sort": "title", "dir": "desc"}):
        swapped = authed_client.get("/chirp", params=params, headers={"HX-Request": "true"}).text
        assert swapped.count('id="chirp-books-table"') == 1
        assert swapped.count("audiobook(s) owned") == 0


def test_sidebar_links_to_the_page(authed_client):
    assert 'href="/chirp"' in authed_client.get("/downloads").text
