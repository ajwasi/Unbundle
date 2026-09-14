"""Downloads a single Audible title's companion PDF (course material/
illustrated-book supplements some titles ship) — a plain authenticated-URL
httpx stream, deliberately NOT built on downloads/worker.py's
Download/DownloadJob machinery: that subsystem is shaped around humble-cli's
gamekey/subproduct-index semantics and shells out to a subprocess, neither
of which applies here. There is no existing plain-httpx-streamed-file-to-disk
code anywhere else in this app to reuse either — Steam/GOG/Audible's other
connectors only ever fetch JSON.

Same "module-level background-task set to prevent GC" pattern
downloads/worker.py documents (_background_tasks there) — an asyncio.Task
with no other live reference can be garbage-collected mid-run, silently
killing the download.
"""

import asyncio
import time
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.downloads.paths import sanitize_dir_name
from app.models.audible_book import AudibleBook
from app.models.audible_pdf_download import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AudiblePdfDownload,
)

_background_tasks: set[asyncio.Task] = set()
_active_asins: set[str] = set()


class NoPdfAvailableError(Exception):
    """Raised when the book has no pdf_url to download."""


class PdfDownloadInProgressError(Exception):
    """Raised when a download for this asin is already running."""


def start_download(db: Session, asin: str) -> AudiblePdfDownload:
    if asin in _active_asins:
        raise PdfDownloadInProgressError(f"A PDF download for {asin} is already in progress.")

    book = db.get(AudibleBook, asin)
    if book is None or not book.pdf_url:
        raise NoPdfAvailableError(f"No PDF is available for {asin}.")

    job = AudiblePdfDownload(asin=asin, status=STATUS_QUEUED)
    db.add(job)
    db.commit()
    db.refresh(job)

    _active_asins.add(asin)
    task = asyncio.create_task(_run(job.id, asin, book.pdf_url, book.title))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return job


def latest_status(db: Session, asin: str) -> AudiblePdfDownload | None:
    return (
        db.query(AudiblePdfDownload)
        .filter(AudiblePdfDownload.asin == asin)
        .order_by(AudiblePdfDownload.id.desc())
        .first()
    )


async def _run(job_id: int, asin: str, pdf_url: str, title: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AudiblePdfDownload, job_id)
        job.status = STATUS_RUNNING
        job.started_at = datetime.utcnow()
        db.commit()

        dest_path = None
        try:
            dest_dir = settings.downloads_dir / "Audible"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / f"{sanitize_dir_name(title)}.pdf"

            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream("GET", pdf_url) as resp:
                    resp.raise_for_status()
                    job.expected_size_bytes = int(resp.headers.get("content-length") or 0) or None
                    db.commit()

                    bytes_written = 0
                    last_committed_at = time.monotonic()
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
                            bytes_written += len(chunk)
                            now = time.monotonic()
                            if now - last_committed_at >= 1.0:
                                job.progress_bytes = bytes_written
                                db.commit()
                                last_committed_at = now
                    job.progress_bytes = bytes_written
        except Exception as exc:  # noqa: BLE001 - surfaced on the job row, never a crash
            job.status = STATUS_FAILED
            job.error_message = str(exc)
            job.completed_at = datetime.utcnow()
            db.commit()
            if dest_path is not None:
                dest_path.unlink(missing_ok=True)
            return

        # Trust the filesystem over the HTTP-reported size, same philosophy
        # downloads/worker.py already uses for humble-cli's own downloads.
        if dest_path.exists() and dest_path.stat().st_size > 0:
            job.status = STATUS_COMPLETED
            job.downloaded_path = str(dest_path)
        else:
            job.status = STATUS_FAILED
            job.error_message = "Downloaded file is missing or empty."
        job.completed_at = datetime.utcnow()
        db.commit()
    finally:
        _active_asins.discard(asin)
        db.close()
