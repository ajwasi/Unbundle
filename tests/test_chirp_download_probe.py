import base64
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.connectors import chirp_connector as chirp
from app.connectors.chirp_connector import ChirpTrack
from app.models.credential import SOURCE_CHIRP, STATUS_OK, Credential
from app.security import encrypt_json
from app.sync import chirp_sync

KEY = b"0123456789abcdef"
USER_ID = 4210139
MEDIA_URL = "https://audio.chirpbooks.com/audio/529663/track.m4a?Expires=1&Signature=abc"


def _encrypt_like_chirp(url: str) -> str:
    # The decrypter drops exactly one trailing byte, so the plaintext needs one
    # extra byte at the end and a whole number of AES blocks.
    body = url.encode()
    pad = (-(len(body) + 1)) % 16
    plaintext = body + b"0" * pad + b"!"
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(chirp.derive_iv(USER_ID))).encryptor()
    return base64.b64encode(encryptor.update(plaintext) + encryptor.finalize()).decode()


def _client(player_html: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "audio.chirpbooks.com":
            return httpx.Response(200, content=b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 16, headers={"content-type": "audio/mp4"})
        return httpx.Response(200, text=player_html)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


PLAYER_HTML = f'<div class="user-audiobook" data-dk="{KEY.decode()}" data-audiobook-id="626000"></div>"userId":{USER_ID}'


async def test_probe_walks_the_chain_and_reports_each_step_without_the_key():
    tracks = [ChirpTrack(part_number=1, chapter_number=1, offset_from_book_start_ms=0, duration_ms=1000, display_name="Ch 1")]
    with (
        patch.object(chirp, "fetch_tracks", new=AsyncMock(return_value=tracks)),
        patch.object(chirp, "fetch_encrypted_track_url", new=AsyncMock(return_value=_encrypt_like_chirp(MEDIA_URL))),
    ):
        async with _client(PLAYER_HTML) as client:
            report = await chirp.probe_first_track(client, "27991647", "626000")

    assert report["key_found"] and report["user_id_found"]
    assert report["track_count"] == 1
    assert report["media_host"] == "audio.chirpbooks.com"
    assert report["media_extension"] == "m4a"
    assert report["media_status"] == 200
    assert report["mp4_box_type_at_offset_4"] == "ftyp"
    assert "stopped_at" not in report
    assert "Signature" not in str(report)
    assert KEY.decode() not in str(report)


async def test_probe_stops_cleanly_when_the_player_page_has_no_key():
    async with _client("<html>no player data</html>") as client:
        report = await chirp.probe_first_track(client, "27991647", "626000")

    assert report["key_found"] is False
    assert "stopped_at" in report


def test_probe_route_reports_not_connected(authed_client):
    resp = authed_client.get("/chirp/probe-download", params={"purchase_id": "27991647", "audiobook_id": "626000"})
    assert resp.status_code == 409


def test_probe_route_returns_the_report(authed_client, db):
    db.add(Credential(source=SOURCE_CHIRP, status=STATUS_OK, encrypted_payload=encrypt_json({"cookie": "cf_clearance=abc"})))
    db.commit()
    report = {"key_found": True, "media_extension": "m4a"}
    with patch.object(chirp, "probe_first_track", new=AsyncMock(return_value=report)):
        resp = authed_client.get("/chirp/probe-download", params={"purchase_id": "27991647", "audiobook_id": "626000"})

    assert resp.status_code == 200
    assert '"media_extension": "m4a"' in resp.text
