import asyncio
import time

import httpx
import pytest

from app.audible import pdf_downloader
from app.models.audible_book import AudibleBook
from app.models.audible_pdf_download import STATUS_COMPLETED, STATUS_FAILED

_POLL_TIMEOUT_SECONDS = 2.0


def _wait_until(predicate, timeout=_POLL_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


async def _await_until(predicate, timeout=_POLL_TIMEOUT_SECONDS):
    # The download itself runs as an asyncio.Task on this same event loop
    # (not a background OS thread, unlike the Audible login bridge) — a
    # synchronous time.sleep() here would starve that task instead of
    # letting it progress, so this awaits asyncio.sleep() instead.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _add_book(db, asin="B001", pdf_url="https://example.com/companion.pdf"):
    db.add(AudibleBook(asin=asin, title="A Great Book", pdf_url=pdf_url))
    db.commit()


def _patch_async_client(monkeypatch, transport):
    # Capture the real class before patching — a naive lambda that calls
    # httpx.AsyncClient(...) inside itself would recurse into the patched
    # name at call time instead of the original.
    original_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: original_async_client(transport=transport))


def test_start_download_raises_when_no_pdf_available(db):
    db.add(AudibleBook(asin="B001", title="No PDF Here", pdf_url=""))
    db.commit()
    with pytest.raises(pdf_downloader.NoPdfAvailableError):
        pdf_downloader.start_download(db, "B001")


def test_start_download_raises_when_book_missing(db):
    with pytest.raises(pdf_downloader.NoPdfAvailableError):
        pdf_downloader.start_download(db, "NONEXISTENT")


@pytest.mark.asyncio
async def test_start_download_completes_and_saves_file(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)

    def handler(request):
        return httpx.Response(200, content=b"%PDF-1.4 fake content")

    transport = httpx.MockTransport(handler)
    _patch_async_client(monkeypatch, transport)

    pdf_downloader.start_download(db, "B001")
    assert await _await_until(lambda: pdf_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = pdf_downloader.latest_status(db, "B001")
    assert job.status == STATUS_COMPLETED
    assert job.downloaded_path
    from pathlib import Path

    assert Path(job.downloaded_path).read_bytes() == b"%PDF-1.4 fake content"


@pytest.mark.asyncio
async def test_start_download_marks_failed_on_http_error(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    _add_book(db)

    def handler(request):
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    _patch_async_client(monkeypatch, transport)

    pdf_downloader.start_download(db, "B001")
    assert await _await_until(lambda: pdf_downloader.latest_status(db, "B001").status in (STATUS_COMPLETED, STATUS_FAILED))

    job = pdf_downloader.latest_status(db, "B001")
    assert job.status == STATUS_FAILED
    assert job.error_message


def test_start_download_rejects_concurrent_download_for_same_asin(db, monkeypatch):
    _add_book(db)
    monkeypatch.setattr(pdf_downloader, "_active_asins", {"B001"})
    with pytest.raises(pdf_downloader.PdfDownloadInProgressError):
        pdf_downloader.start_download(db, "B001")


def test_latest_status_none_when_never_attempted(db):
    _add_book(db)
    assert pdf_downloader.latest_status(db, "B001") is None
