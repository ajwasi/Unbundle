from unittest.mock import AsyncMock, patch

from app.connectors import chirp_connector as chirp
from app.connectors.chirp_connector import ChirpAudiobook, ChirpRequestError
from app.models.credential import SOURCE_CHIRP, STATUS_OK, Credential
from app.security import encrypt_json


def _connect(db, cookie="cf_clearance=abc; _mockingjay_session=xyz"):
    db.add(Credential(source=SOURCE_CHIRP, status=STATUS_OK, encrypted_payload=encrypt_json({"cookie": cookie})))
    db.commit()


def _book(**overrides) -> ChirpAudiobook:
    defaults = dict(
        purchase_id="1",
        audiobook_id="626000",
        title="Wool",
        authors="Hugh Howey",
        narrators="Edoardo Ballerini",
        url_path="/audiobooks/wool-by-hugh-howey-4415b4a1b5",
        cover_url="https://img.chirpbooks.com/x.jpg",
        progress_status="IN_PROGRESS",
        position_percent=14,
        playable=True,
        series_name="The Silo Saga",
        series_number="1",
    )
    defaults.update(overrides)
    return ChirpAudiobook(**defaults)


def test_page_requires_auth(client):
    assert client.get("/chirp", follow_redirects=False).status_code == 303


def test_check_route_requires_auth(client):
    assert client.post("/chirp/check", follow_redirects=False).status_code == 303


def test_page_points_at_settings_when_not_connected(authed_client):
    resp = authed_client.get("/chirp")
    assert "isn't connected yet" in resp.text or "isn&#39;t connected yet" in resp.text
    assert '<a href="/settings">' in resp.text


def test_page_shows_check_button_when_connected(authed_client, db):
    _connect(db)
    resp = authed_client.get("/chirp")
    assert "Check library" in resp.text


def test_check_library_shows_books_on_success(authed_client, db):
    _connect(db)
    with patch.object(chirp, "fetch_library_preview_via_cookie", new=AsyncMock(return_value=([_book()], 78))):
        resp = authed_client.post("/chirp/check")

    assert resp.status_code == 200
    assert "Wool" in resp.text
    assert "Hugh Howey" in resp.text
    assert "78 audiobook" in resp.text


def test_check_library_without_a_connection_shows_a_settings_prompt(authed_client, db):
    resp = authed_client.post("/chirp/check")
    assert resp.status_code == 200
    assert "Settings" in resp.text
    assert "isn" in resp.text.lower()  # "isn't connected"


def test_check_library_surfaces_a_request_error(authed_client, db):
    _connect(db)
    with patch.object(chirp, "fetch_library_preview_via_cookie", new=AsyncMock(side_effect=ChirpRequestError("bad query"))):
        resp = authed_client.post("/chirp/check")
    assert "bad query" in resp.text


def test_check_library_surfaces_an_unexpected_error_without_500ing(authed_client, db):
    _connect(db)
    with patch.object(chirp, "fetch_library_preview_via_cookie", new=AsyncMock(side_effect=RuntimeError("boom"))):
        resp = authed_client.post("/chirp/check")
    assert resp.status_code == 200
    assert "RuntimeError" in resp.text


def test_check_library_handles_an_empty_library(authed_client, db):
    _connect(db)
    with patch.object(chirp, "fetch_library_preview_via_cookie", new=AsyncMock(return_value=([], 0))):
        resp = authed_client.post("/chirp/check")
    assert "No audiobooks found" in resp.text


def test_htmx_request_returns_only_the_content(authed_client, db):
    _connect(db)
    resp = authed_client.get("/chirp", headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert 'id="chirp-content"' in resp.text
