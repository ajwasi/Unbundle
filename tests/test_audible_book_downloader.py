import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.audible import book_downloader
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import STATUS_COMPLETED, STATUS_FAILED
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


def _add_book(db, asin="B001", title="A Great Book"):
    db.add(AudibleBook(asin=asin, title=title))
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


def test_start_download_raises_when_book_missing(db):
    with pytest.raises(book_downloader.NoAudiobookAvailableError):
        book_downloader.start_download(db, "NONEXISTENT")


def test_start_download_rejects_concurrent_download_for_same_asin(db, monkeypatch):
    _add_book(db)
    monkeypatch.setattr(book_downloader, "_active_asins", {"B001"})
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
    # the intermediate .aaxc is cleaned up once the m4b exists
    assert not (tmp_path / "Audible" / "A Great Book.aaxc").exists()


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
    aaxc_path = tmp_path / "Audible" / "A Great Book.aaxc"
    voucher_path = tmp_path / "Audible" / "A Great Book.voucher.json"
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
