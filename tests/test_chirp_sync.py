import json
from datetime import date, datetime
from unittest.mock import patch

import httpx
import pytest

from app.connectors import chirp_connector as chirp
from app.models.chirp_audiobook import ChirpAudiobook
from app.models.credential import SOURCE_CHIRP, STATUS_ERROR, Credential
from app.security import encrypt_json
from app.sync import chirp_sync


_EMPTY_ORDER_HISTORY = '<div class="purchases-list"></div>'


def _connect(db, cookie="cf_clearance=abc"):
    db.add(Credential(source=SOURCE_CHIRP, encrypted_payload=encrypt_json({"cookie": cookie})))
    db.commit()


def _cookie_client_stub(handler):
    def build(cookie_header: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Cookie": cookie_header}, follow_redirects=True)

    return build


def _page_response(book_ids: list, total: int) -> dict:
    return {
        "data": {
            "currentUserAudiobooks": [
                {
                    "id": bid,
                    "progressStatus": "IN_PROGRESS",
                    "positionPercent": 0,
                    "playable": True,
                    "audiobook": {
                        "id": bid,
                        "url": f"/audiobooks/{bid}",
                        "coverUrl": "",
                        "displayTitle": f"Book {bid}",
                        "displayAuthors": "",
                        "displayNarrators": "",
                        "seriesAudiobook": None,
                        "currentProduct": None,
                    },
                }
                for bid in book_ids
            ],
            "currentUserAudiobooksCount": total,
        }
    }


async def test_refresh_requires_a_connection(db):
    with pytest.raises(chirp_sync.NotConnectedError):
        await chirp_sync.refresh_chirp_library(db)


async def test_refresh_walks_every_page_until_the_total_is_reached(db):
    _connect(db)
    pages = {
        1: _page_response([str(i) for i in range(1, 21)], 45),
        2: _page_response([str(i) for i in range(21, 41)], 45),
        3: _page_response([str(i) for i in range(41, 46)], 45),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/purchases":
            return httpx.Response(200, text=_EMPTY_ORDER_HISTORY)
        page_num = json.loads(request.content)["variables"]["page"]
        return httpx.Response(200, json=pages[page_num])

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        count = await chirp_sync.refresh_chirp_library(db)

    assert count == 45
    assert db.query(ChirpAudiobook).count() == 45


async def test_refresh_stops_if_a_page_stops_introducing_new_ids(db):
    """Defends against `page` being silently ignored server-side: if every
    page returns the same ids, the walk must stop rather than loop forever
    or double-count the same books."""
    _connect(db)
    same_page = _page_response([str(i) for i in range(1, 21)], 78)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/purchases":
            return httpx.Response(200, text=_EMPTY_ORDER_HISTORY)
        return httpx.Response(200, json=same_page)

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        count = await chirp_sync.refresh_chirp_library(db)

    # Only the first page's worth — the confirmed total (78) is never reached,
    # and that's the correct, honest outcome when pagination isn't advancing.
    assert count == 20


async def test_refresh_replaces_stale_rows_no_longer_in_the_library(db):
    db.add(ChirpAudiobook(purchase_id="stale", title="Old Book", fetched_at=datetime.utcnow()))
    db.commit()
    _connect(db)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/purchases":
            return httpx.Response(200, text=_EMPTY_ORDER_HISTORY)
        return httpx.Response(200, json=_page_response(["1"], 1))

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        await chirp_sync.refresh_chirp_library(db)

    assert db.query(ChirpAudiobook).filter(ChirpAudiobook.purchase_id == "stale").one_or_none() is None
    assert db.query(ChirpAudiobook).filter(ChirpAudiobook.purchase_id == "1").one_or_none() is not None


async def test_refresh_attaches_the_earliest_purchase_date_and_paid_price(db):
    from pathlib import Path

    _connect(db)
    html = (Path(__file__).parent / "fixtures" / "chirp_purchases_page.html").read_text(encoding="utf-8")
    library = {
        "data": {
            "currentUserAudiobooks": [
                {
                    "id": "27991647",
                    "progressStatus": "IN_PROGRESS",
                    "positionPercent": 0,
                    "playable": True,
                    "audiobook": {
                        "id": "626000",
                        "url": "/audiobooks/sand-by-hugh-howey-f6379e10a9",
                        "coverUrl": "",
                        "displayTitle": "Sand",
                        "displayAuthors": "Hugh Howey",
                        "displayNarrators": "Jeremy Arthur",
                        "seriesAudiobook": None,
                        "currentProduct": None,
                    },
                },
                {
                    "id": "99999999",
                    "progressStatus": "NOT_STARTED",
                    "positionPercent": 0,
                    "playable": True,
                    "audiobook": {
                        "id": "1",
                        "url": "/audiobooks/never-bought",
                        "coverUrl": "",
                        "displayTitle": "Never Bought",
                        "displayAuthors": "",
                        "displayNarrators": "",
                        "seriesAudiobook": None,
                        "currentProduct": None,
                    },
                },
            ],
            "currentUserAudiobooksCount": 2,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/purchases":
            return httpx.Response(200, text=html)
        return httpx.Response(200, json=library)

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        await chirp_sync.refresh_chirp_library(db)

    sand = db.query(ChirpAudiobook).filter(ChirpAudiobook.purchase_id == "27991647").one()
    assert sand.purchased_at == date(2025, 12, 1)
    assert sand.paid_price == 7.99

    never = db.query(ChirpAudiobook).filter(ChirpAudiobook.purchase_id == "99999999").one()
    assert never.purchased_at is None
    assert never.paid_price is None


async def test_refresh_records_the_error_on_failure(db):
    _connect(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    with patch.object(chirp, "client_from_cookie_header", new=_cookie_client_stub(handler)):
        with pytest.raises(chirp.ChirpRequestError):
            await chirp_sync.refresh_chirp_library(db)

    cred = Credential.get(db, SOURCE_CHIRP)
    assert cred.status == STATUS_ERROR
