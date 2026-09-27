import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.amazon_music import downloader
from app.connectors import amazon_music_connector as amc
from app.connectors import amazon_music_template as tmpl
from app.models.amazon_music_destination import AmazonMusicDestination
from app.models.amazon_music_download import STATUS_COMPLETED, STATUS_FAILED, STATUS_RUNNING
from app.models.amazon_music_track import AmazonMusicTrack
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.security import encrypt_json

_POLL_TIMEOUT_SECONDS = 3.0

SIGNED_URL = (
    "https://d1l04yptno92u8.cloudfront.net/DigitalMusicDeliveryService/CloudDriveEmbed.mp3"
    "?e=1789868665&cid=A2HOWQQO9HOTF7&cdoid=c64eeeb1-e203-4c0a-9213-43ac6202c74a"
    "&isrc=GBAAM8300001&tid=111-1008484-5274644&pt=1566309461277&h=5aa57bcecd5576474b20a63a"
)


async def _await_until(db, predicate, timeout=_POLL_TIMEOUT_SECONDS):
    # The background drain runs on its own SessionLocal(), a different
    # session than the caller's `db` fixture — without expiring `db`'s
    # identity map on every poll, it would keep returning the same
    # in-memory row exactly as it looked the moment this session first
    # loaded it, never seeing the background task's later commits at all.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _connect_audible(db):
    db.add(
        Credential(
            source=SOURCE_AUDIBLE,
            status=STATUS_OK,
            encrypted_payload=encrypt_json({"website_cookies": {"at-main": "x"}, "locale_code": "us"}),
        )
    )
    db.commit()


def _save_template(db):
    tmpl.save_template(
        db,
        tmpl.SyncTemplate(
            path=amc.PURCHASED_TRACKS_PATH,
            headers_field=json.dumps({"x-amzn-authentication": json.dumps({"accessToken": "OLD"})}),
            user_hash="{}",
            captured_at="now",
        ),
    )


def _add_track(db, download_id="c64eeeb1-e203-4c0a-9213-43ac6202c74a", **kwargs):
    defaults = dict(
        download_id=download_id,
        track_asin="B076HFF4Q3",
        title="Every Breath You Take",
        artist="The Police",
        album="Synchronicity",
        first_seen_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    db.add(AmazonMusicTrack(**defaults))
    db.commit()


class _Auth:
    website_cookies = {"at-main": "x"}

    @classmethod
    def from_dict(cls, data):
        return cls()


def _handler_factory(download_response=None, delivery_content=b"FAKE MP3 BYTES", delivery_status=200):
    download_response = download_response or {"url": SIGNED_URL}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("config.json"):
            return httpx.Response(
                200,
                json={
                    "accessToken": "Atna|NEW",
                    "deviceId": "DEV",
                    "deviceType": "TYPE",
                    "sessionId": "SESS",
                    "montanaCsrf": "CSRF",
                },
            )
        if request.url.path == amc.DOWNLOAD_TRACK_PATH:
            return httpx.Response(200, json=download_response)
        host = request.url.host or ""
        if host == "cloudfront.net" or host.endswith(".cloudfront.net"):
            return httpx.Response(delivery_status, content=delivery_content)
        return httpx.Response(404)

    return handler


def _patch_async_client(monkeypatch, transport):
    original_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: original_async_client(transport=transport, **{}))


def _reset_module_state(monkeypatch):
    monkeypatch.setattr(downloader, "_queue_running", False)
    monkeypatch.setattr(downloader, "_current_job_id", None)


# --------------------------------------------------------------- queueing

def test_start_download_raises_for_an_unknown_download_id(db):
    with pytest.raises(downloader.UnknownTrackError):
        asyncio.run(downloader.start_download(db, "NOT-A-REAL-ID"))


@pytest.mark.asyncio
async def test_queue_many_skips_unknown_ids_and_reports_the_real_count(db, monkeypatch):
    _reset_module_state(monkeypatch)
    _add_track(db, download_id="id-1")
    _add_track(db, download_id="id-2", track_asin="B07BFJR1HC", title="The Dance")

    # No stored Audible login: the background drain this triggers fails fast
    # and safely (see the dedicated test below) — irrelevant to what this
    # test checks, which is only queue_many's own return value.
    queued = await downloader.queue_many(db, ["id-1", "id-2", "not-a-real-id"])

    assert queued == 2


# ------------------------------------------------------ destination paths

def test_resolve_destination_root_falls_back_when_none_configured(db):
    from app.config import settings

    assert downloader.resolve_destination_root(db) == settings.downloads_dir / "Amazon Music"


def test_resolve_destination_root_uses_the_default_row(db, tmp_path):
    db.add(AmazonMusicDestination(name="NAS", path=str(tmp_path / "music"), is_default=True))
    db.add(AmazonMusicDestination(name="Other", path=str(tmp_path / "other"), is_default=False))
    db.commit()

    assert downloader.resolve_destination_root(db) == tmp_path / "music"


def test_compilations_route_to_a_various_artists_folder(db, tmp_path):
    _add_track(db, is_compilation=True, artist="Some Artist")
    track = db.get(AmazonMusicTrack, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
    dest_dir = downloader._track_dest_dir(tmp_path, track)
    assert dest_dir == tmp_path / "Various Artists" / "Synchronicity"


def test_guess_extension_reads_the_real_signed_url_path():
    assert downloader._guess_extension(SIGNED_URL) == ".mp3"


def test_guess_extension_falls_back_to_mp3_when_the_url_has_none():
    assert downloader._guess_extension("https://example.invalid/no-extension-here") == ".mp3"


# --------------------------------------------------------- full downloads

@pytest.mark.asyncio
async def test_start_download_completes_and_saves_the_file(db, tmp_path, monkeypatch):
    from app.config import settings

    _reset_module_state(monkeypatch)
    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _connect_audible(db)
    _save_template(db)
    _add_track(db)

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler_factory()))

    with patch("audible.Authenticator", _Auth):
        job = await downloader.start_download(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
        assert await _await_until(
            db,
            lambda: downloader.latest_status(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a").status
            in (STATUS_COMPLETED, STATUS_FAILED),
        )

    db.expire_all()
    finished = downloader.latest_status(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
    assert finished.id == job.id
    assert finished.status == STATUS_COMPLETED
    assert finished.downloaded_path
    saved = Path(finished.downloaded_path)
    assert saved.read_bytes() == b"FAKE MP3 BYTES"
    assert saved.suffix == ".mp3"
    # The Police / Synchronicity, not a compilation — its own artist folder.
    assert saved.parent == tmp_path / "Amazon Music" / "The Police" / "Synchronicity"


@pytest.mark.asyncio
async def test_a_downloadtrack_error_response_fails_the_job_with_amazons_own_text(db, tmp_path, monkeypatch):
    from app.config import settings

    _reset_module_state(monkeypatch)
    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _connect_audible(db)
    _save_template(db)
    _add_track(db)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("config.json"):
            return httpx.Response(200, json={"accessToken": "Atna|NEW"})
        if request.url.path == amc.DOWNLOAD_TRACK_PATH:
            return httpx.Response(400, text="Missing required header x-amzn-csrf")
        return httpx.Response(404)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))

    with patch("audible.Authenticator", _Auth):
        await downloader.start_download(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
        assert await _await_until(
            db, lambda: downloader.latest_status(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a").status == STATUS_FAILED
        )

    db.expire_all()
    job = downloader.latest_status(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
    assert "x-amzn-csrf" in job.error_message


@pytest.mark.asyncio
async def test_a_response_with_no_delivery_url_fails_cleanly(db, tmp_path, monkeypatch):
    from app.config import settings

    _reset_module_state(monkeypatch)
    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _connect_audible(db)
    _save_template(db)
    _add_track(db)

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler_factory(download_response={"template": {}})))

    with patch("audible.Authenticator", _Auth):
        await downloader.start_download(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
        assert await _await_until(
            db, lambda: downloader.latest_status(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a").status == STATUS_FAILED
        )

    db.expire_all()
    job = downloader.latest_status(db, "c64eeeb1-e203-4c0a-9213-43ac6202c74a")
    assert "delivery URL" in job.error_message


@pytest.mark.asyncio
async def test_no_audible_login_fails_the_whole_queued_batch_without_retrying_each(db, tmp_path, monkeypatch):
    """The real failure mode this guards: without an account-level check up
    front, N queued tracks with no Audible connection would each individually
    attempt (and fail) a session build, one full TRACK_DOWNLOAD_DELAY_SECONDS
    apart — making a "download all" on a disconnected account take as long as
    a real one before saying so. This proves both queued rows fail together
    from one session-build attempt.
    """
    from app.config import settings

    _reset_module_state(monkeypatch)
    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    monkeypatch.setattr(downloader, "TRACK_DOWNLOAD_DELAY_SECONDS", 0)
    _add_track(db, download_id="id-1")
    _add_track(db, download_id="id-2", track_asin="B07BFJR1HC", title="The Dance")
    # Deliberately no _connect_audible(db) — no stored login at all.

    await downloader.queue_many(db, ["id-1", "id-2"])
    assert await _await_until(
        db,
        lambda: downloader.latest_status(db, "id-1").status == STATUS_FAILED
        and downloader.latest_status(db, "id-2").status == STATUS_FAILED,
    )

    db.expire_all()
    job1 = downloader.latest_status(db, "id-1")
    job2 = downloader.latest_status(db, "id-2")
    assert "Audible" in job1.error_message
    assert job1.error_message == job2.error_message
    # Neither ever reached STATUS_RUNNING — the account-level failure is
    # caught before any per-track work starts.
    assert job1.started_at is None
    assert job2.started_at is None


# --------------------------------------------------------------- restart

def test_sweep_stale_downloads_fails_anything_left_running(db, monkeypatch):
    _reset_module_state(monkeypatch)
    _add_track(db)
    from app.models.amazon_music_download import AmazonMusicDownload

    stuck = AmazonMusicDownload(download_id="c64eeeb1-e203-4c0a-9213-43ac6202c74a", status=STATUS_RUNNING)
    db.add(stuck)
    db.commit()

    downloader.sweep_stale_downloads()

    db.refresh(stuck)
    assert stuck.status == STATUS_FAILED
    assert "restart" in stuck.error_message
