from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.models.audible_book import AudibleBook
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json


def _connect_audible(db):
    db.add(Credential(source=SOURCE_AUDIBLE, encrypted_payload=encrypt_json({"access_token": "AT"}), status=STATUS_OK))
    db.commit()


def test_audible_page_requires_auth(client):
    resp = client.get("/audible", follow_redirects=False)
    assert resp.status_code == 303


def test_audible_page_shows_not_configured_prompt(authed_client):
    resp = authed_client.get("/audible")
    assert resp.status_code == 200
    assert "isn't connected" in resp.text


def test_audible_page_shows_books_and_stats(authed_client, db):
    _connect_audible(db)
    db.add(AudibleBook(asin="B001", title="A Great Book", author="Jane Author", runtime_minutes=605))
    db.commit()

    resp = authed_client.get("/audible")
    assert "A Great Book" in resp.text
    assert "Jane Author" in resp.text
    assert "1 book(s) owned" in resp.text
    assert "10.1 hour(s)" in resp.text


def test_audible_page_shows_never_refreshed_with_no_books(authed_client, db):
    _connect_audible(db)
    resp = authed_client.get("/audible")
    assert "Never refreshed" in resp.text


def test_audible_page_shows_last_refreshed_time(authed_client, db):
    _connect_audible(db)
    fetched_at = datetime.utcnow() - timedelta(hours=2)
    db.add(AudibleBook(asin="B001", title="A Great Book", fetched_at=fetched_at))
    db.commit()

    resp = authed_client.get("/audible")
    assert "Last refreshed" in resp.text
    assert "2 hours ago" in resp.text


def test_refresh_audible_triggers_sync_and_rerenders(authed_client, db):
    _connect_audible(db)

    async def _fake_refresh(_db):
        db.add(AudibleBook(asin="B001", title="A Great Book", author="", runtime_minutes=0))
        db.commit()
        return 1

    with patch("app.routers.audible.audible_sync.refresh_audible_library", new=AsyncMock(side_effect=_fake_refresh)):
        resp = authed_client.post("/audible/refresh")
    assert resp.status_code == 200
    assert "A Great Book" in resp.text


def test_refresh_audible_when_not_connected_does_not_crash(authed_client):
    resp = authed_client.post("/audible/refresh")
    assert resp.status_code == 200


def test_audible_page_shows_ownership_badge(authed_client, db):
    _connect_audible(db)
    db.add(AudibleBook(asin="B001", title="Owned Book", benefit_id="LIBRARY"))
    db.add(AudibleBook(asin="B002", title="Plus Catalog Book", benefit_id="AYCL"))
    db.commit()

    resp = authed_client.get("/audible")
    assert "Owned" in resp.text
    assert "Plus Catalog" in resp.text
    assert "audible-row-credit" in resp.text


def test_audible_page_search_matches_title_or_author(authed_client, db):
    _connect_audible(db)
    db.add(AudibleBook(asin="B001", title="A Great Book", author="Jane Author", runtime_minutes=0))
    db.add(AudibleBook(asin="B002", title="Another Title", author="Someone Else", runtime_minutes=0))
    db.commit()

    resp_by_title = authed_client.get("/audible", params={"q": "great"})
    assert "A Great Book" in resp_by_title.text
    assert "Another Title" not in resp_by_title.text

    resp_by_author = authed_client.get("/audible", params={"q": "jane"})
    assert "A Great Book" in resp_by_author.text
    assert "Another Title" not in resp_by_author.text
    # Library-wide stats stay the true total, unaffected by the search filter.
    assert "2 book(s) owned" in resp_by_author.text


def test_audible_page_owned_filter(authed_client, db):
    # Titles deliberately avoid the substring "Owned Book" — the page's own
    # always-present "Owned Books (N)" header would make that a false-positive
    # match regardless of which filter is active.
    _connect_audible(db)
    db.add(AudibleBook(asin="B001", title="Library Title", benefit_id="LIBRARY"))
    db.add(AudibleBook(asin="B002", title="Freebie Title", benefit_id="AYCL"))
    db.commit()

    resp_owned = authed_client.get("/audible", params={"owned": "owned"})
    assert "Library Title" in resp_owned.text
    assert "Freebie Title" not in resp_owned.text

    resp_plus = authed_client.get("/audible", params={"owned": "plus"})
    assert "Freebie Title" in resp_plus.text
    assert "Library Title" not in resp_plus.text


def test_audible_page_sort_by_price(authed_client, db):
    _connect_audible(db)
    db.add(AudibleBook(asin="B001", title="Cheap Book", price_amount=5.0))
    db.add(AudibleBook(asin="B002", title="Pricey Book", price_amount=50.0))
    db.commit()

    resp_asc = authed_client.get("/audible", params={"sort": "price", "dir": "asc"})
    assert resp_asc.text.index("Cheap Book") < resp_asc.text.index("Pricey Book")

    resp_desc = authed_client.get("/audible", params={"sort": "price", "dir": "desc"})
    assert resp_desc.text.index("Pricey Book") < resp_desc.text.index("Cheap Book")


def test_audible_page_clear_filters_link_only_shown_when_filtered(authed_client, db):
    _connect_audible(db)
    db.commit()

    assert "Clear filters" not in authed_client.get("/audible").text
    assert "Clear filters" in authed_client.get("/audible", params={"owned": "owned"}).text


def test_audible_page_hx_request_returns_just_the_table(authed_client, db):
    _connect_audible(db)
    db.add(AudibleBook(asin="B001", title="A Great Book", author="Jane Author", runtime_minutes=0))
    db.commit()

    resp = authed_client.get("/audible", headers={"HX-Request": "true"})
    assert 'id="audible-books-table"' in resp.text
    assert "A Great Book" in resp.text
    assert "<h1>Audible Library</h1>" not in resp.text


def test_audible_detail_requires_auth(client):
    resp = client.get("/audible/B001", follow_redirects=False)
    assert resp.status_code == 303


def test_audible_detail_404_for_unknown_asin(authed_client, db):
    resp = authed_client.get("/audible/NONEXISTENT")
    assert resp.status_code == 404


def test_audible_detail_shows_book_metadata(authed_client, db):
    db.add(
        AudibleBook(
            asin="B001",
            title="A Great Book",
            author="Jane Author",
            series_title="The Great Series",
            series_sequence="3",
            rating_average=4.5,
            price_amount=14.99,
            price_currency="USD",
            benefit_id="LIBRARY",
        )
    )
    db.commit()

    resp = authed_client.get("/audible/B001")
    assert resp.status_code == 200
    assert "A Great Book" in resp.text
    assert "The Great Series" in resp.text
    assert "Owned" in resp.text
    assert "14.99" in resp.text


def test_audible_detail_shows_pdf_section_only_when_pdf_available(authed_client, db):
    db.add(AudibleBook(asin="B001", title="No PDF", pdf_url=""))
    db.add(AudibleBook(asin="B002", title="Has PDF", pdf_url="https://example.com/x.pdf"))
    db.commit()

    resp_no_pdf = authed_client.get("/audible/B001")
    assert "Companion PDF" not in resp_no_pdf.text

    resp_pdf = authed_client.get("/audible/B002")
    assert "Companion PDF" in resp_pdf.text
    assert "Download PDF" in resp_pdf.text


def test_start_pdf_download_route(authed_client, db):
    db.add(AudibleBook(asin="B001", title="Has PDF", pdf_url="https://example.com/x.pdf"))
    db.commit()

    resp = authed_client.post("/audible/B001/pdf/download")
    assert resp.status_code == 200


def test_start_pdf_download_route_handles_no_pdf_gracefully(authed_client, db):
    db.add(AudibleBook(asin="B001", title="No PDF", pdf_url=""))
    db.commit()

    resp = authed_client.post("/audible/B001/pdf/download")
    assert resp.status_code == 200


def test_pdf_download_status_route(authed_client, db):
    db.add(AudibleBook(asin="B001", title="Has PDF", pdf_url="https://example.com/x.pdf"))
    db.commit()

    resp = authed_client.get("/audible/B001/pdf/status")
    assert resp.status_code == 200


def test_download_pdf_file_route_404s_without_a_completed_download(authed_client, db):
    db.add(AudibleBook(asin="B001", title="Has PDF", pdf_url="https://example.com/x.pdf"))
    db.commit()

    resp = authed_client.get("/audible/B001/pdf/file")
    assert resp.status_code == 404
