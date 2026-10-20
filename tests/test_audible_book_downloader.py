import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.audible import book_downloader
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import STATUS_COMPLETED, STATUS_FAILED, STATUS_QUEUED, AudibleBookDownload
from app.models.audible_download_destination import AudibleDownloadDestination
from app.models.credential import SOURCE_AUDIBLE, Credential
from app.security import encrypt_json

_POLL_TIMEOUT_SECONDS = 2.0

LICENSE_RESPONSE = {
    "content_license": {
        "asin": "B001",
        "content_metadata": {"content_url": {"offline_url": "https://cds.audible.com/fake.aaxc"}},
        "license_response": "fake-encrypted-voucher",
    }
}


async def _await_until(predicate, timeout=_POLL_TIMEOUT_SECONDS):
    # The download itself runs as an asyncio.Task on this same event loop
    # (see app/audible/pdf_downloader.py's own test for why this awaits
    # asyncio.sleep() rather than time.sleep()ing, which would starve it).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _add_book(db, asin="B001", title="A Great Book", author="A Great Author"):
    db.add(AudibleBook(asin=asin, title=title, author=author))
    db.commit()


def _connect(db):
    db.add(Credential(source=SOURCE_AUDIBLE, encrypted_payload=encrypt_json({"access_token": "AT", "locale_code": "us"})))
    db.commit()


def _patch_async_client(monkeypatch, transport):
    original_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: original_async_client(transport=transport))


class _FakeAuth:
    def to_dict(self):
        return {"access_token": "AT", "locale_code": "us"}


def _aaxc_handler(request):
    return httpx.Response(200, content=b"fake-aaxc-bytes")


def test_book_folder_rel_path_is_author_then_title():
    assert book_downloader._book_folder_rel_path("Jane Author", "Cool Book") == Path("Jane Author") / "Cool Book"


def test_book_folder_rel_path_falls_back_when_author_blank():
    assert book_downloader._book_folder_rel_path("", "Cool Book") == Path("Unknown Author") / "Cool Book"


def test_start_download_raises_when_book_missing(db):
    with pytest.raises(book_downloader.NoAudiobookAvailableError):
        book_downloader.start_download(db, "NONEXISTENT")


def test_start_download_rejects_concurrent_download_for_same_asin(db):
    _add_book(db)
    db.add(AudibleBookDownload(asin="B001", status=STATUS_QUEUED))
    db.commit()
    with pytest.raises(book_downloader.BookDownloadInProgressError):
        book_downloader.start_download(db, "B001")


def test_latest_status_none_when_never_attempted(db):
    _add_book(db)
    assert book_downloader.latest_status(db, "B001") is None


@pytest.mark.asyncio
async def test_start_download_completes_and_remuxes_to_m4b(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    _connect(db)
    _patch_async_client(monkeypatch, httpx.MockTransport(_aaxc_handler))

    async def fake_remux(aaxc_path, m4b_path, key, iv):
        assert key == "thekey"
        assert iv == "theiv"
        Path(m4b_path).write_bytes(b"fake-m4b-bytes")
        return True, ""

    with (
        patch.object(book_downloader.audible.Authenticator, "from_dict", return_value=_FakeAuth()),
        patch("app.audible.book_downloader.audible_connector.request_license", new=AsyncMock(return_value=LICENSE_RESPONSE)),
        patch("app.audible.book_downloader.decrypt_voucher_from_licenserequest", return_value={"key": "thekey", "iv": "theiv"}),
        patch("app.audible.book_downloader._remux_to_m4b", new=fake_remux),
    ):
        book_downloader.start_download(db, "B001")
        assert await _await_until(lambda: book_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = book_downloader.latest_status(db, "B001")
    assert job.status == STATUS_COMPLETED
    assert job.downloaded_path.endswith(".m4b")
    assert Path(job.downloaded_path).read_bytes() == b"fake-m4b-bytes"
    # lands at Author/Title/Title.m4b — the layout Plex/AudiobookShelf expect,
    # not a bare file dropped in the destination root
    assert job.downloaded_path == str(tmp_path / "Audible" / "A Great Author" / "A Great Book" / "A Great Book.m4b")
    # the intermediate .aaxc is cleaned up once the m4b exists
    assert not (tmp_path / "Audible" / "A Great Author" / "A Great Book" / "A Great Book.aaxc").exists()
    # and the finished file is remembered, so it's never downloaded again
    from app.models.audible_local_file import AudibleLocalFile

    db.expire_all()
    assert db.get(AudibleLocalFile, "B001").path == job.downloaded_path


@pytest.mark.asyncio
async def test_start_download_keeps_raw_files_when_ffmpeg_fails(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    _connect(db)
    _patch_async_client(monkeypatch, httpx.MockTransport(_aaxc_handler))

    async def failing_remux(aaxc_path, m4b_path, key, iv):
        return False, "ffmpeg exploded"

    with (
        patch.object(book_downloader.audible.Authenticator, "from_dict", return_value=_FakeAuth()),
        patch("app.audible.book_downloader.audible_connector.request_license", new=AsyncMock(return_value=LICENSE_RESPONSE)),
        patch("app.audible.book_downloader.decrypt_voucher_from_licenserequest", return_value={"key": "k", "iv": "i"}),
        patch("app.audible.book_downloader._remux_to_m4b", new=failing_remux),
    ):
        book_downloader.start_download(db, "B001")
        assert await _await_until(lambda: book_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = book_downloader.latest_status(db, "B001")
    assert job.status == STATUS_FAILED
    assert "ffmpeg exploded" in job.error_message
    assert "aaxc" in job.error_message.lower()
    # the raw file and its decryption key are kept, not thrown away
    aaxc_path = tmp_path / "Audible" / "A Great Author" / "A Great Book" / "A Great Book.aaxc"
    voucher_path = tmp_path / "Audible" / "A Great Author" / "A Great Book" / "A Great Book.voucher.json"
    assert aaxc_path.exists()
    assert aaxc_path.read_bytes() == b"fake-aaxc-bytes"
    assert voucher_path.exists()


@pytest.mark.asyncio
async def test_start_download_fails_cleanly_when_license_has_no_url(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    _connect(db)

    with (
        patch.object(book_downloader.audible.Authenticator, "from_dict", return_value=_FakeAuth()),
        patch("app.audible.book_downloader.audible_connector.request_license", new=AsyncMock(return_value={"content_license": {}})),
    ):
        book_downloader.start_download(db, "B001")
        assert await _await_until(lambda: book_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = book_downloader.latest_status(db, "B001")
    assert job.status == STATUS_FAILED
    assert "download URL" in job.error_message


@pytest.mark.asyncio
async def test_start_download_surfaces_audibles_own_denial_reasons(db, tmp_path, monkeypatch):
    """Real-account shape (2026-10-19): a denied license is still an HTTP 200,
    with the actual reason only present in content_license.license_denial_reasons.
    Before this, that information was thrown away and the job just said "no
    download URL" — leaving the user with no idea why."""
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    _connect(db)

    denied_response = {
        "content_license": {
            "status_code": "Denied",
            "license_denial_reasons": [
                {"message": "Customer is not part of any plans", "rejectionReason": "RequesterEligibility", "validationType": "Membership"},
                {"message": "Asin: [B001] is not eligible for AYCL", "rejectionReason": "ContentEligibility", "validationType": "AYCL"},
            ],
        }
    }

    with (
        patch.object(book_downloader.audible.Authenticator, "from_dict", return_value=_FakeAuth()),
        patch("app.audible.book_downloader.audible_connector.request_license", new=AsyncMock(return_value=denied_response)),
    ):
        book_downloader.start_download(db, "B001")
        assert await _await_until(lambda: book_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = book_downloader.latest_status(db, "B001")
    assert job.status == STATUS_FAILED
    assert "not part of any plans" in job.error_message
    assert "not eligible for AYCL" in job.error_message
    assert "amazon.com/hz/mycd/digital-console/devicedetails" in job.error_message


@pytest.mark.asyncio
async def test_start_download_records_the_real_error_even_if_cleanup_unlink_fails(db, tmp_path, monkeypatch):
    """Regression test for a real incident: a destination mounted read-only
    made the post-failure `aaxc_path.unlink()` itself raise OSError (errno
    30), which — before safe_unlink — propagated straight out of the task
    uncaught (surfacing only as asyncio's "Task exception was never
    retrieved"), even though the *real* error ("no download URL") had
    already been committed to the job row by that point. The job must still
    end up STATUS_FAILED with that real message, not hang at "running".
    """
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    _connect(db)

    with (
        patch.object(book_downloader.audible.Authenticator, "from_dict", return_value=_FakeAuth()),
        patch("app.audible.book_downloader.audible_connector.request_license", new=AsyncMock(return_value={"content_license": {}})),
        patch("pathlib.Path.unlink", side_effect=OSError(30, "Read-only file system")),
    ):
        book_downloader.start_download(db, "B001")
        assert await _await_until(lambda: book_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = book_downloader.latest_status(db, "B001")
    assert job.status == STATUS_FAILED
    assert "download URL" in job.error_message


@pytest.mark.asyncio
async def test_start_download_fails_when_not_connected(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    # no Credential row saved — not connected

    book_downloader.start_download(db, "B001")
    assert await _await_until(lambda: book_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = book_downloader.latest_status(db, "B001")
    assert job.status == STATUS_FAILED
    assert "not connected" in job.error_message.lower()


@pytest.mark.asyncio
async def test_remux_to_m4b_reports_a_missing_ffmpeg_binary():
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
        ok, message = await book_downloader._remux_to_m4b("in.aaxc", "out.m4b", "k", "i")
    assert ok is False
    assert "not installed" in message


@pytest.mark.asyncio
async def test_remux_to_m4b_passes_key_and_iv_before_the_input_flag():
    captured = {}

    class _FakeProc:
        returncode = 0

        class stdout:
            @staticmethod
            async def read():
                return b""

        async def wait(self):
            return None

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc()

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        ok, message = await book_downloader._remux_to_m4b("in.aaxc", "out.m4b", "thekey", "theiv")

    assert ok is True
    args = captured["args"]
    assert args[0] == "ffmpeg"
    i_index = args.index("-i")
    key_index = args.index("-audible_key")
    assert key_index < i_index  # input-specific options must precede -i
    assert args[key_index + 1] == "thekey"
    iv_index = args.index("-audible_iv")
    assert iv_index < i_index
    assert args[iv_index + 1] == "theiv"
    assert args[i_index + 1] == "in.aaxc"
    assert "out.m4b" in args


@pytest.mark.asyncio
async def test_remux_to_m4b_reports_a_nonzero_exit():
    class _FakeProc:
        returncode = 1

        class stdout:
            @staticmethod
            async def read():
                return b"Unknown option audible_key"

        async def wait(self):
            return None

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        ok, message = await book_downloader._remux_to_m4b("in.aaxc", "out.m4b", "k", "i")

    assert ok is False
    assert "Unknown option audible_key" in message


def test_resolve_destination_root_falls_back_to_the_zero_config_default(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    assert book_downloader.resolve_destination_root(db) == tmp_path / "Audible"


def test_resolve_destination_root_prefers_the_configured_default(db, tmp_path):
    db.add(AudibleDownloadDestination(name="NAS", path=str(tmp_path / "nas-audiobooks"), is_default=True))
    db.add(AudibleDownloadDestination(name="Other", path=str(tmp_path / "unused"), is_default=False))
    db.commit()
    assert book_downloader.resolve_destination_root(db) == tmp_path / "nas-audiobooks"


def test_start_download_reports_already_downloaded_without_a_new_license_request(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    dest_dir = tmp_path / "Audible" / "A Great Author" / "A Great Book"
    dest_dir.mkdir(parents=True)
    existing = dest_dir / "A Great Book.m4b"
    existing.write_bytes(b"already here")

    request_license = AsyncMock()
    with patch("app.audible.book_downloader.audible_connector.request_license", new=request_license):
        job = book_downloader.start_download(db, "B001")

    assert job.status == STATUS_COMPLETED
    assert job.downloaded_path == str(existing)
    request_license.assert_not_called()


@pytest.mark.asyncio
async def test_start_download_force_redownloads_even_when_a_file_already_exists(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)
    _connect(db)
    dest_dir = tmp_path / "Audible" / "A Great Author" / "A Great Book"
    dest_dir.mkdir(parents=True)
    (dest_dir / "A Great Book.m4b").write_bytes(b"stale copy")
    _patch_async_client(monkeypatch, httpx.MockTransport(_aaxc_handler))

    async def fake_remux(aaxc_path, m4b_path, key, iv):
        Path(m4b_path).write_bytes(b"fresh copy")
        return True, ""

    with (
        patch.object(book_downloader.audible.Authenticator, "from_dict", return_value=_FakeAuth()),
        patch("app.audible.book_downloader.audible_connector.request_license", new=AsyncMock(return_value=LICENSE_RESPONSE)),
        patch("app.audible.book_downloader.decrypt_voucher_from_licenserequest", return_value={"key": "k", "iv": "i"}),
        patch("app.audible.book_downloader._remux_to_m4b", new=fake_remux),
    ):
        book_downloader.start_download(db, "B001", force=True)
        assert await _await_until(lambda: book_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = book_downloader.latest_status(db, "B001")
    assert job.status == STATUS_COMPLETED
    assert Path(job.downloaded_path).read_bytes() == b"fresh copy"
