"""Downloads a single Audible title's actual audiobook, end to end:

1. License request (audible_connector.request_license) — the `audible`
   package's own official example, see that function's own docstring.
2. Stream the resulting .aaxc to disk — a plain authenticated-URL httpx
   stream, same shape as pdf_downloader.py, but with the User-Agent the
   official example sends (Audible's own iOS app's UA; unconfirmed whether
   this is load-bearing or just the example's own convention, kept rather
   than guessed away).
3. Decrypt the license voucher (audible.aescipher) to get this file's own
   AES key/iv.
4. Remux (not re-encode — a plain stream copy) into a DRM-free .m4b via
   ffmpeg's built-in AAXC demuxer, using that key/iv.

On an ffmpeg failure specifically (binary missing, or a real conversion
error), the downloaded .aaxc and a decrypted-voucher .json are kept on disk
rather than deleted — the same raw-file-plus-key fallback a user could have
chosen outright (see the Settings-page discussion this was built from), so
a conversion problem never loses the one thing that's expensive to re-fetch
(the license is single-use-ish and re-requesting one burns an Audible-side
download credit for some titles).

Queued exactly like downloads/worker.py queues Humble bundles: any number can
be requested, up to the configured concurrency limit (the same Downloads-page
setting) run at once, the rest wait as STATUS_QUEUED. Where a finished file
lives is also remembered in AudibleLocalFile (as is anything a folder scan
finds — see audible/scan.py), so a book already on disk is never fetched
again.
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import audible
from audible.aescipher import decrypt_voucher_from_licenserequest
import httpx
from sqlalchemy.orm import Session

from app.asyncio_utils import spawn_background_task
from app.config import settings
from app.connectors import audible_connector
from app.db import SessionLocal
from app.downloads import worker
from app.downloads.paths import resolve_within, sanitize_dir_name
from app.fs_utils import safe_unlink
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AudibleBookDownload,
)
from app.models.audible_download_destination import AudibleDownloadDestination
from app.models.audible_local_file import SOURCE_DOWNLOAD, AudibleLocalFile
from app.sync.audible_sync import NotConnectedError, get_audible_credential, save_authenticator

# Audible's own iOS app's User-Agent, per the `audible` package's official
# download example — unconfirmed whether the download URL actually requires
# it (vs. any client working), kept because the one example known to work
# sends it.
_DOWNLOAD_USER_AGENT = "Audible/671 CFNetwork/1240.0.4 Darwin/20.6.0"

# Job ids currently executing — bounded by the configured concurrency limit;
# everything else waits in the DB as STATUS_QUEUED. Plain module state, not
# persisted, for the same single-process reason as downloads/worker.py.
_running_job_ids: set[int] = set()
# job_id -> {"bytes_per_sec"} for whatever's downloading right now. Byte
# counts themselves live on the job row (progress_bytes), already committed
# about once a second by _download_aaxc.
_progress: dict[int, dict] = {}


class NoAudiobookAvailableError(Exception):
    """Raised when the book row can't be found."""


class BookDownloadInProgressError(Exception):
    """Raised when a download for this asin is already queued or running."""


def resolve_destination_root(db: Session) -> Path:
    row = db.query(AudibleDownloadDestination).filter(AudibleDownloadDestination.is_default.is_(True)).first()
    if row is not None:
        return Path(row.path)
    # Zero-config fallback, same convention pdf_downloader.py's own hardcoded
    # "Audible" subfolder already uses — downloading works before anyone
    # visits the destinations picker, it just lands somewhere less
    # deliberately chosen. Deliberately not shared with pdf_downloader.py
    # itself (see AudibleDownloadDestination's own docstring).
    return settings.downloads_dir / "Audible"


def _is_present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def record_local_file(db: Session, asin: str, path: Path, source: str = SOURCE_DOWNLOAD) -> None:
    """Upserts where this book's file lives. Commits."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    row = db.get(AudibleLocalFile, asin)
    if row is None:
        row = AudibleLocalFile(asin=asin)
        db.add(row)
    row.path = str(path)
    row.size_bytes = size
    row.source = source
    row.recorded_at = datetime.utcnow()
    db.commit()


def _book_folder_rel_path(author: str, title: str) -> Path:
    """Author/Title — one folder per book, named by its author — the layout
    both Plex's audiobook library type and AudiobookShelf expect in order to
    recognise a book at all (a bare file dropped straight in the root isn't
    enough for either). Shared by _run() (to build this download's landing
    folder) and existing_file() (to keep looking in the right place for one
    already there) so the two can never drift apart.
    """
    return Path(sanitize_dir_name(author or "Unknown Author")) / sanitize_dir_name(title)


def existing_file(db: Session, book: AudibleBook) -> Path | None:
    """The file this book already has on disk, if any. Trusts the filesystem
    over the stored record, same philosophy as the Humble worker's re-verify
    and the Amazon Music downloader's _is_completed_and_present: a file
    deleted outside this app since it was recorded should download again,
    not stay silently skipped forever. Checks the remembered location first
    (a scan can find a book anywhere), then where this exact download would
    land.
    """
    local = db.get(AudibleLocalFile, book.asin)
    if local is not None and _is_present(Path(local.path)):
        return Path(local.path)
    expected = (
        resolve_destination_root(db)
        / _book_folder_rel_path(book.author, book.title)
        / f"{sanitize_dir_name(book.title)}.m4b"
    )
    return expected if _is_present(expected) else None


def _is_in_progress(db: Session, asin: str) -> bool:
    return (
        db.query(AudibleBookDownload.id)
        .filter(AudibleBookDownload.asin == asin, AudibleBookDownload.status.in_((STATUS_QUEUED, STATUS_RUNNING)))
        .first()
        is not None
    )


def start_download(db: Session, asin: str, force: bool = False) -> AudibleBookDownload:
    """Queues one title. Never raises for "the queue is busy" — whether this
    runs now or waits behind others is try_dispatch_queued_downloads's call,
    made right after (same convention as downloads/worker.py).

    A license request isn't free to redo (it burns an Audible-side download
    credit for some titles, per the module docstring) and running ffmpeg
    again on a file that's already right is pure waste — if this book is
    already on disk, report it as already downloaded instead of repeating
    all of that. "Re-download" in the UI sets force=True specifically to
    bypass this and get a fresh copy.
    """
    if _is_in_progress(db, asin):
        raise BookDownloadInProgressError(f"An audiobook download for {asin} is already in progress.")

    book = db.get(AudibleBook, asin)
    if book is None:
        raise NoAudiobookAvailableError(f"No Audible book found for {asin}.")

    existing_path = None if force else existing_file(db, book)
    if existing_path is not None:
        job = AudibleBookDownload(
            asin=asin,
            status=STATUS_COMPLETED,
            downloaded_path=str(existing_path),
            expected_size_bytes=existing_path.stat().st_size,
            progress_bytes=existing_path.stat().st_size,
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    job = AudibleBookDownload(asin=asin, status=STATUS_QUEUED)
    db.add(job)
    db.commit()
    db.refresh(job)
    try_dispatch_queued_downloads()
    return job


def queue_many(db: Session, asins: list[str], force: bool = False) -> dict[str, list[str]]:
    """Queues every title that exists, isn't already queued/running, and
    isn't already on disk (unless force). Returns the asins by outcome:
    queued / already_downloaded / in_progress / unknown — "Download selected"
    reports these back instead of silently dropping anything.
    """
    outcome: dict[str, list[str]] = {"queued": [], "already_downloaded": [], "in_progress": [], "unknown": []}
    books = {b.asin: b for b in db.query(AudibleBook).filter(AudibleBook.asin.in_(asins)).all()}
    for asin in dict.fromkeys(asins):
        book = books.get(asin)
        if book is None:
            outcome["unknown"].append(asin)
        elif _is_in_progress(db, asin):
            outcome["in_progress"].append(asin)
        elif not force and existing_file(db, book) is not None:
            outcome["already_downloaded"].append(asin)
        else:
            db.add(AudibleBookDownload(asin=asin, status=STATUS_QUEUED))
            db.flush()
            outcome["queued"].append(asin)
    db.commit()
    if outcome["queued"]:
        try_dispatch_queued_downloads()
    return outcome


def latest_status(db: Session, asin: str) -> AudibleBookDownload | None:
    return (
        db.query(AudibleBookDownload)
        .filter(AudibleBookDownload.asin == asin)
        .order_by(AudibleBookDownload.id.desc())
        .first()
    )


def get_active_jobs(db: Session) -> list[dict]:
    """Every running or queued audiobook download, for the Downloads page's
    Active section — same shape downloads/worker.py's get_active_jobs() gives
    for bundles (bytes_done / bytes_total / bytes_per_sec).
    """
    rows = (
        db.query(AudibleBookDownload, AudibleBook.title)
        .outerjoin(AudibleBook, AudibleBook.asin == AudibleBookDownload.asin)
        .filter(AudibleBookDownload.status.in_((STATUS_RUNNING, STATUS_QUEUED)))
        .order_by(AudibleBookDownload.id)
        .all()
    )
    return [
        {
            "id": job.id,
            "asin": job.asin,
            "title": title or job.asin,
            "status": job.status,
            "bytes_done": job.progress_bytes,
            "bytes_total": job.expected_size_bytes or 0,
            "bytes_per_sec": _progress.get(job.id, {}).get("bytes_per_sec", 0.0),
        }
        for job, title in rows
    ]


def sweep_stale_downloads() -> None:
    """Call on app startup — a restart mid-download would otherwise leave a
    row stuck 'running' forever (same reasoning as worker.sweep_stale_jobs).
    Queued rows are left alone; try_dispatch_queued_downloads picks them up.
    """
    _running_job_ids.clear()
    _progress.clear()
    db = SessionLocal()
    try:
        for job in db.query(AudibleBookDownload).filter(AudibleBookDownload.status == STATUS_RUNNING).all():
            job.status = STATUS_FAILED
            job.completed_at = datetime.utcnow()
            job.error_message = "Interrupted by application restart."
        db.commit()
    finally:
        db.close()


def try_dispatch_queued_downloads() -> None:
    """Starts as many queued downloads as the concurrency limit allows. Called
    after anything is queued and again after one finishes — no polling loop.
    The limit is the same Downloads-page setting Humble bundles use, re-read
    every call so a change takes effect on the next dispatch (the two queues
    each apply it to their own running set).

    Claims a job (marks it running and commits) before spawning its task, so
    the next loop iteration can't be handed the same row; there's no await
    between the limit check and the claim, so two calls can't over-claim.
    """
    db = SessionLocal()
    try:
        limit = worker._current_concurrency_limit(db)
        while len(_running_job_ids) < limit:
            job = (
                db.query(AudibleBookDownload)
                .filter(AudibleBookDownload.status == STATUS_QUEUED)
                .order_by(AudibleBookDownload.id)
                .first()
            )
            if job is None:
                break
            job.status = STATUS_RUNNING
            job.started_at = datetime.utcnow()
            book = db.get(AudibleBook, job.asin)
            if book is None:
                job.status = STATUS_FAILED
                job.error_message = "This book is no longer in your Audible library."
                job.completed_at = datetime.utcnow()
                db.commit()
                continue
            db.commit()
            _running_job_ids.add(job.id)
            spawn_background_task(_run(job.id, job.asin, book.title, book.author))
    finally:
        db.close()


def _extract_offline_url(license_response: dict) -> str | None:
    try:
        return license_response["content_license"]["content_metadata"]["content_url"]["offline_url"]
    except (KeyError, TypeError):
        return None


# Where Amazon lists every device registered against this account (filtered
# to the Audible app) — the one place a stale/duplicate registration can
# actually be deregistered, which a plain reconnect in this app's own
# Settings page does not do (that only adds a new registration, it never
# removes an old one). Confirmed independently by Libation
# (github.com/rmcrackan/Libation — AppScaffolding/LicenseRecoveryGuidance.cs)
# as the fix for this same denial shape after an Audible-side change broke
# license grants for a lot of accounts.
_DEVICE_MANAGEMENT_URL = "https://www.amazon.com/hz/mycd/digital-console/devicedetails?deviceFamily=AUDIBLE_APP"


def _denial_error_message(license_response: dict) -> str | None:
    """A license request that Audible denies is still an HTTP 200 — the
    denial lives entirely in content_license.license_denial_reasons, which
    _extract_offline_url has no reason to look at (there's no content_url on
    a denied response either way). Without this, the job's error_message was
    just "no download URL", throwing away the one piece of information
    Audible actually gave: why.

    None when there's nothing to report (an empty/absent
    license_denial_reasons) — the caller falls back to its own generic
    message for that case, same as before this existed.
    """
    reasons = (license_response.get("content_license") or {}).get("license_denial_reasons") or []
    if not isinstance(reasons, list) or not reasons:
        return None

    details = "; ".join(
        f"{r.get('validationType')}: {r.get('message')}" for r in reasons if isinstance(r, dict) and r.get("message")
    )
    if not details:
        return None

    return (
        f"Audible denied the license for this title ({details}). This is Audible's own decision, "
        "not an error in this app — most often a stale device registration at Amazon's end, or a "
        "real account/plan eligibility limit. Try deregistering old entries at "
        f"{_DEVICE_MANAGEMENT_URL} (look under the Audible app), then disconnect and reconnect "
        "Audible in Settings and retry. If it still fails, wait 24-48 hours (Audible also applies "
        "temporary rate limits) and confirm the title plays in Audible's own app."
    )


async def _download_aaxc(url: str, dest_path, job: AudibleBookDownload, db: Session) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("GET", url, headers={"User-Agent": _DOWNLOAD_USER_AGENT}) as resp:
            resp.raise_for_status()
            job.expected_size_bytes = int(resp.headers.get("content-length") or 0) or None
            db.commit()

            bytes_written = 0
            last_committed_at = time.monotonic()
            last_bytes = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    bytes_written += len(chunk)
                    now = time.monotonic()
                    if now - last_committed_at >= 1.0:
                        job.progress_bytes = bytes_written
                        db.commit()
                        _progress[job.id] = {"bytes_per_sec": (bytes_written - last_bytes) / (now - last_committed_at)}
                        last_bytes = bytes_written
                        last_committed_at = now
            job.progress_bytes = bytes_written


async def _remux_to_m4b(aaxc_path, m4b_path, key: str, iv: str) -> tuple[bool, str]:
    """Returns (ok, error_output). A missing ffmpeg binary and a real
    conversion failure are both reported through this same tuple — the
    caller's job.error_message is the only place either needs to surface."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-audible_key", key,
            "-audible_iv", iv,
            "-i", str(aaxc_path),
            "-c", "copy",
            str(m4b_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, "ffmpeg is not installed in this container."

    output = await proc.stdout.read()
    await proc.wait()
    if proc.returncode != 0:
        return False, output.decode(errors="replace")[-2000:]
    return True, ""


async def _run(job_id: int, asin: str, title: str, author: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AudibleBookDownload, job_id)

        # None until the try block below actually creates the destination
        # folder — the except block only cleans up what might exist, and
        # resolve_within (a PathTraversalError, in practice never expected
        # from Audible's own catalog data) can fail before anything does.
        aaxc_path = voucher_path = None

        try:
            dest_dir = resolve_within(resolve_destination_root(db), _book_folder_rel_path(author, title))
            dest_dir.mkdir(parents=True, exist_ok=True)
            base_name = sanitize_dir_name(title)
            aaxc_path = dest_dir / f"{base_name}.aaxc"
            voucher_path = dest_dir / f"{base_name}.voucher.json"
            m4b_path = dest_dir / f"{base_name}.m4b"

            payload = get_audible_credential(db)
            if not payload:
                raise NotConnectedError("Audible is not connected yet — connect it in Settings.")
            auth = audible.Authenticator.from_dict(dict(payload))

            license_response = await audible_connector.request_license(auth, asin)
            save_authenticator(db, auth)

            offline_url = _extract_offline_url(license_response)
            if not offline_url:
                raise ValueError(
                    _denial_error_message(license_response)
                    or "Audible's license response did not include a download URL."
                )

            await _download_aaxc(offline_url, aaxc_path, job, db)

            voucher = decrypt_voucher_from_licenserequest(auth, license_response)
            key, iv = voucher.get("key"), voucher.get("iv")
            if not key or not iv:
                raise ValueError("The decrypted license voucher is missing its key/iv.")
            voucher_path.write_text(json.dumps(voucher, indent=2))
        except Exception as exc:  # noqa: BLE001 - surfaced on the job row, never a crash
            job.status = STATUS_FAILED
            job.error_message = str(exc)
            job.completed_at = datetime.utcnow()
            db.commit()
            if aaxc_path is not None:
                safe_unlink(aaxc_path)
            if voucher_path is not None:
                safe_unlink(voucher_path)
            return

        ok, ffmpeg_output = await _remux_to_m4b(aaxc_path, m4b_path, key, iv)
        if ok and m4b_path.exists() and m4b_path.stat().st_size > 0:
            # The .aaxc is DRM-locked to this one license and useless on its
            # own once the m4b exists — the voucher stays too (cheap, and
            # the only record of the key/iv that produced this exact file).
            # Best-effort: a cleanup failure here must never cost the job
            # its own completed status — the m4b already exists and is
            # done, regardless of whether the now-redundant .aaxc could be
            # removed (confirmed live: a destination mounted read-only
            # left a job stuck at "running" forever otherwise, despite a
            # fully finished download sitting right there).
            safe_unlink(aaxc_path)
            job.status = STATUS_COMPLETED
            job.downloaded_path = str(m4b_path)
            record_local_file(db, asin, m4b_path)
        else:
            job.status = STATUS_FAILED
            job.error_message = (
                f"ffmpeg conversion failed; the raw .aaxc and its decryption key were kept at "
                f"{aaxc_path} / {voucher_path}. {ffmpeg_output}".strip()
            )
        job.completed_at = datetime.utcnow()
        db.commit()
    finally:
        _running_job_ids.discard(job_id)
        _progress.pop(job_id, None)
        db.close()
        try_dispatch_queued_downloads()
