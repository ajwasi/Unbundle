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
