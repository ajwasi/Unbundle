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
from app.downloads.paths import sanitize_dir_name
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AudibleBookDownload,
)
from app.models.audible_download_destination import AudibleDownloadDestination
from app.sync.audible_sync import NotConnectedError, get_audible_credential, save_authenticator

# Audible's own iOS app's User-Agent, per the `audible` package's official
# download example — unconfirmed whether the download URL actually requires
# it (vs. any client working), kept because the one example known to work
# sends it.
_DOWNLOAD_USER_AGENT = "Audible/671 CFNetwork/1240.0.4 Darwin/20.6.0"

_active_asins: set[str] = set()


class NoAudiobookAvailableError(Exception):
    """Raised when the book row can't be found."""


class BookDownloadInProgressError(Exception):
    """Raised when a download for this asin is already running."""


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


def start_download(db: Session, asin: str, force: bool = False) -> AudibleBookDownload:
    if asin in _active_asins:
        raise BookDownloadInProgressError(f"An audiobook download for {asin} is already in progress.")

    book = db.get(AudibleBook, asin)
    if book is None:
        raise NoAudiobookAvailableError(f"No Audible book found for {asin}.")

    # A license request isn't free to redo (it burns an Audible-side download
    # credit for some titles, per the module docstring) and running ffmpeg
    # again on a file that's already right is pure waste — if the file this
    # exact download would produce already exists, report it as already
    # downloaded instead of repeating all of that. "Re-download" in the UI
    # sets force=True specifically to bypass this and get a fresh copy.
    existing_path = resolve_destination_root(db) / f"{sanitize_dir_name(book.title)}.m4b"
    if not force and existing_path.exists() and existing_path.stat().st_size > 0:
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

    _active_asins.add(asin)
    spawn_background_task(_run(job.id, asin, book.title))
    return job


def latest_status(db: Session, asin: str) -> AudibleBookDownload | None:
    return (
        db.query(AudibleBookDownload)
        .filter(AudibleBookDownload.asin == asin)
        .order_by(AudibleBookDownload.id.desc())
        .first()
    )


def _extract_offline_url(license_response: dict) -> str | None:
    try:
        return license_response["content_license"]["content_metadata"]["content_url"]["offline_url"]
    except (KeyError, TypeError):
        return None


async def _download_aaxc(url: str, dest_path, job: AudibleBookDownload, db: Session) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("GET", url, headers={"User-Agent": _DOWNLOAD_USER_AGENT}) as resp:
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


async def _run(job_id: int, asin: str, title: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AudibleBookDownload, job_id)
        job.status = STATUS_RUNNING
        job.started_at = datetime.utcnow()
        db.commit()

        dest_dir = resolve_destination_root(db)
        dest_dir.mkdir(parents=True, exist_ok=True)
        base_name = sanitize_dir_name(title)
        aaxc_path = dest_dir / f"{base_name}.aaxc"
        voucher_path = dest_dir / f"{base_name}.voucher.json"
        m4b_path = dest_dir / f"{base_name}.m4b"

        try:
            payload = get_audible_credential(db)
            if not payload:
                raise NotConnectedError("Audible is not connected yet — connect it in Settings.")
            auth = audible.Authenticator.from_dict(dict(payload))

            license_response = await audible_connector.request_license(auth, asin)
            save_authenticator(db, auth)

            offline_url = _extract_offline_url(license_response)
            if not offline_url:
                raise ValueError("Audible's license response did not include a download URL.")

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
            aaxc_path.unlink(missing_ok=True)
            voucher_path.unlink(missing_ok=True)
            return

        ok, ffmpeg_output = await _remux_to_m4b(aaxc_path, m4b_path, key, iv)
        if ok and m4b_path.exists() and m4b_path.stat().st_size > 0:
            # The .aaxc is DRM-locked to this one license and useless on its
            # own once the m4b exists — the voucher stays too (cheap, and
            # the only record of the key/iv that produced this exact file).
            aaxc_path.unlink(missing_ok=True)
            job.status = STATUS_COMPLETED
            job.downloaded_path = str(m4b_path)
        else:
            job.status = STATUS_FAILED
            job.error_message = (
                f"ffmpeg conversion failed; the raw .aaxc and its decryption key were kept at "
                f"{aaxc_path} / {voucher_path}. {ffmpeg_output}".strip()
            )
        job.completed_at = datetime.utcnow()
        db.commit()
    finally:
        _active_asins.discard(asin)
        db.close()
