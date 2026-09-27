"""Downloads purchased tracks' audio files via POST /api/downloadTrack.

The request shape is a confident extension of a confirmed pattern, not a
fresh guess: every other endpoint in this API family (showPurchasedTracks,
confirmed 2026-09-20) takes the exact same envelope — a JSON body of
{"headers": <replayed x-amzn-* template, freshened>, "userHash": ...} sent
under Content-Type text/plain — and this module's own model docstring
already confirms download_id is what downloadTrack takes as its `id`. What
is NOT independently confirmed is the *whole* request shape for this
specific endpoint; if Amazon rejects it, the fix is the same as every other
shape mismatch in this connector has been — a fresh DevTools capture of a
real downloadTrack call, not another guess. The response is easier ground:
find_signed_download_url() searches for the delivery URL by its own
unmistakable shape (a cloudfront.net host, a cdoid query param) rather than
assuming which key wraps it, which is only "something like 'url'" per the
one real capture on file.

Runs as a single sequential queue, not many tracks in parallel — hitting an
undocumented API on a personal account that many times at once is not
something "download my whole library" should ever do just because it can.
The session (cookies, freshened headers_field, user_hash) is built once per
queue drain and reused across every track in that drain, both because
rebuilding it per track would mean minting Amazon website cookies and
fetching config.json once per track for no benefit, and because a session
failure (no Audible connected, no template saved, stale login) is an
account-level problem — failing every queued track individually with the
same message, one slow retry at a time, would make "download all" on a
disconnected account take as long as the real thing before saying so.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.asyncio_utils import spawn_background_task
from app.config import settings
from app.connectors import amazon_music_connector as amc
from app.connectors import amazon_music_probe as probe
from app.connectors import amazon_music_template as tmpl
from app.connectors.amazon_music_probe import NotConnectedError as _ProbeNotConnectedError
from app.db import SessionLocal
from app.downloads.paths import sanitize_dir_name
from app.models.amazon_music_destination import AmazonMusicDestination
from app.models.amazon_music_download import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AmazonMusicDownload,
)
from app.models.amazon_music_track import AmazonMusicTrack
from app.sync.amazon_music_sync import AmazonMusicRequestError, NoTemplateError, NotConnectedError

# Same courtesy as amazon_music_sync.PAGE_DELAY_SECONDS, applied between
# tracks instead of between pages.
TRACK_DOWNLOAD_DELAY_SECONDS = 1.5

# Guards against a second drain loop starting while one is already running —
# this app is single-process (see main.py's own "never add --workers"
# comment), so plain module state is enough, same reasoning as
# downloads/worker.py's _running_job_ids.
_queue_running = False
_current_job_id: int | None = None


class UnknownTrackError(Exception):
    """The download_id doesn't match any track this app has on file."""


def is_download_running() -> bool:
    return _queue_running


def get_active_downloads(db: Session) -> list[dict]:
    """Every running or queued download, for the Amazon Music page's
    active-downloads section — mirrors downloads/worker.py's
    get_active_jobs()."""
    rows = (
        db.query(AmazonMusicDownload, AmazonMusicTrack)
        .join(AmazonMusicTrack, AmazonMusicTrack.download_id == AmazonMusicDownload.download_id)
        .filter(AmazonMusicDownload.status.in_((STATUS_RUNNING, STATUS_QUEUED)))
        .order_by(AmazonMusicDownload.id)
        .all()
    )
    return [
        {
            "id": job.id,
            "download_id": job.download_id,
            "title": track.title,
            "artist": track.artist,
            "status": job.status,
            "progress_bytes": job.progress_bytes,
            "expected_size_bytes": job.expected_size_bytes,
        }
        for job, track in rows
    ]


def latest_status(db: Session, download_id: str) -> AmazonMusicDownload | None:
    return (
        db.query(AmazonMusicDownload)
        .filter(AmazonMusicDownload.download_id == download_id)
        .order_by(AmazonMusicDownload.id.desc())
        .first()
    )


def sweep_stale_downloads() -> None:
    """Call on app startup — a container restart mid-download would otherwise
    leave a row stuck 'running' forever (same reasoning as
    downloads/worker.sweep_stale_jobs). Also resets the in-process
    "currently draining" guards, which are plain module state and don't
    survive a restart either.
    """
    global _queue_running, _current_job_id
    _queue_running = False
    _current_job_id = None
    db = SessionLocal()
    try:
        stale = db.query(AmazonMusicDownload).filter(AmazonMusicDownload.status == STATUS_RUNNING).all()
        for job in stale:
            job.status = STATUS_FAILED
            job.completed_at = datetime.utcnow()
            job.error_message = "Interrupted by application restart."
        db.commit()
    finally:
        db.close()


def resolve_destination_root(db: Session) -> Path:
    row = db.query(AmazonMusicDestination).filter(AmazonMusicDestination.is_default.is_(True)).first()
    if row is not None:
        return Path(row.path)
    # Zero-config fallback, same convention as audible/pdf_downloader.py's
    # own hardcoded "Audible" subfolder — downloading works before anyone
    # visits Settings, it just lands somewhere less deliberately chosen.
    return settings.downloads_dir / "Amazon Music"


def _track_dest_dir(dest_root: Path, track: AmazonMusicTrack) -> Path:
    # is_compilation already exists specifically for this (see its own
    # docstring on AmazonMusicTrack) — an album whose tracks disagree about
    # the artist goes under one shared folder instead of scattering across
    # each track's own (unhelpful) per-row artist.
    artist_dir = "Various Artists" if track.is_compilation else (track.artist or "Unknown Artist")
    album_dir = track.album or "Unknown Album"
    return dest_root / sanitize_dir_name(artist_dir) / sanitize_dir_name(album_dir)


def _guess_extension(delivery_url: str) -> str:
    """The real file extension, read off the signed URL Amazon actually
    handed back, not assumed — every capture on file happens to be .mp3, but
    there's no reason to hardcode that when the URL already says so.
    """
    try:
        suffix = Path(httpx.URL(delivery_url).path).suffix
    except Exception:
        suffix = ""
    return suffix or ".mp3"


async def _build_session(db: Session) -> tuple[httpx.AsyncClient, str, str]:
    """Returns (client, headers_field, user_hash). Caller owns the client and
    must close it. Async, unlike amazon_music_sync's own session setup,
    because downloads run as background asyncio tasks rather than inside a
    request handler FastAPI already runs in a threadpool — see that module's
    docstring for why the whole captured template is replayed instead of
    headers assembled from scratch, and why the access token alone isn't
    enough.
    """
    try:
        cookies = await asyncio.to_thread(probe.cookies_from_audible_credential, db)
    except _ProbeNotConnectedError as exc:
        raise NotConnectedError("Audible isn't connected, so there's no Amazon login to download with.") from exc

    template = tmpl.load_template(db)
    if template is None:
        raise NoTemplateError(
            "No sync template saved yet. Capture the Purchased view request and save it "
            "in Settings before downloading."
        )

    client = httpx.AsyncClient(cookies=cookies, headers=probe.BASE_HEADERS, timeout=30.0, follow_redirects=True)
    try:
        resp = await client.get(amc.config_url())
        if resp.status_code in (401, 403):
            raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
        resp.raise_for_status()
        config = resp.json()
        token = config.get("accessToken") or ""
        if not token:
            raise amc.AmazonMusicAuthError(
                "Amazon did not return an access token — the stored Audible login may have expired."
            )
        headers_field = tmpl.with_fresh_session(template.headers_field, token, probe.session_fields_from_config(config))
        return client, headers_field, template.user_hash
    except Exception:
        await client.aclose()
        raise


async def _fetch_download_url(client: httpx.AsyncClient, headers_field: str, user_hash: str, download_id: str) -> str:
    fields = {"headers": headers_field, "userHash": user_hash, "id": download_id}
    resp = await client.post(
        f"{amc.api_base()}{amc.DOWNLOAD_TRACK_PATH}",
        content=json.dumps(fields),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    if resp.status_code in (401, 403):
        raise amc.AmazonMusicAuthError("Amazon rejected the stored login. Reconnect Audible in Settings.")
    if resp.status_code >= 400:
        detail = (resp.text or "").strip()[:400]
        raise AmazonMusicRequestError(f"Amazon returned {resp.status_code} for {amc.DOWNLOAD_TRACK_PATH}: {detail}")
    resp.raise_for_status()
    url = amc.find_signed_download_url(resp.json())
    if not url:
        raise AmazonMusicRequestError(
            "Amazon's response to downloadTrack didn't contain a delivery URL — the response shape "
            "may not match what this app expects."
        )
    return url


async def start_download(db: Session, download_id: str) -> AmazonMusicDownload:
    """Queues one track. Always queues — never raises for "already busy" —
    same convention as downloads/worker.py's start_download; whether this
    runs immediately or waits behind others is try_dispatch_queued_downloads's
    call, made right after.
    """
    if db.get(AmazonMusicTrack, download_id) is None:
        raise UnknownTrackError(f"No track on file for download_id {download_id!r}.")

    job = AmazonMusicDownload(download_id=download_id)
    db.add(job)
    db.commit()
    db.refresh(job)
    await try_dispatch_queued_downloads()
    return job


async def queue_many(db: Session, download_ids: list[str]) -> int:
    """Queues every id that matches a real, known track — silently skipping
    ones that don't (e.g. a stale checkbox from before a re-sync), the same
    way a single unknown id would just never match anything rather than
    erroring out a whole bulk request over one bad row. Returns how many were
    actually queued.
    """
    known = {
        row[0]
        for row in db.query(AmazonMusicTrack.download_id).filter(AmazonMusicTrack.download_id.in_(download_ids)).all()
    }
    queued = 0
    for download_id in download_ids:
        if download_id in known:
            db.add(AmazonMusicDownload(download_id=download_id))
            queued += 1
    db.commit()
    if queued:
        await try_dispatch_queued_downloads()
    return queued


async def try_dispatch_queued_downloads() -> None:
    """Starts the queue-drain loop if it isn't already running. Safe to call
    any number of times — queuing more tracks while a drain is already in
    progress, or on startup — a call while one is already running is a
    no-op; the loop itself keeps pulling the next queued row until none
    remain.
    """
    global _queue_running
    if _queue_running:
        return
    _queue_running = True
    spawn_background_task(_drain_queue())


async def _drain_queue() -> None:
    global _queue_running, _current_job_id
    client: httpx.AsyncClient | None = None
    try:
        db = SessionLocal()
        try:
            if db.query(AmazonMusicDownload).filter(AmazonMusicDownload.status == STATUS_QUEUED).first() is None:
                return
            try:
                client, headers_field, user_hash = await _build_session(db)
            except Exception as exc:
                # An account-level failure (no login, no template, expired
                # session) fails the whole queued batch at once with the same
                # message, rather than retrying the same broken session once
                # per track — see the module docstring.
                now = datetime.utcnow()
                for job in db.query(AmazonMusicDownload).filter(AmazonMusicDownload.status == STATUS_QUEUED).all():
                    job.status = STATUS_FAILED
                    job.error_message = str(exc)
                    job.completed_at = now
                db.commit()
                return
        finally:
            db.close()

        while True:
            db = SessionLocal()
            try:
                next_job = (
                    db.query(AmazonMusicDownload)
                    .filter(AmazonMusicDownload.status == STATUS_QUEUED)
                    .order_by(AmazonMusicDownload.id)
                    .first()
                )
                if next_job is None:
                    return
                job_id, download_id = next_job.id, next_job.download_id
            finally:
                db.close()

            _current_job_id = job_id
            await _run_one(job_id, download_id, client, headers_field, user_hash)
            _current_job_id = None
            await asyncio.sleep(TRACK_DOWNLOAD_DELAY_SECONDS)
    finally:
        if client is not None:
            await client.aclose()
        _current_job_id = None
        _queue_running = False


async def _run_one(job_id: int, download_id: str, client: httpx.AsyncClient, headers_field: str, user_hash: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AmazonMusicDownload, job_id)
        job.status = STATUS_RUNNING
        job.started_at = datetime.utcnow()
        db.commit()

        dest_path: Path | None = None
        try:
            track = db.get(AmazonMusicTrack, download_id)
            if track is None:
                raise UnknownTrackError("This track is no longer on file — try syncing again.")

            url = await _fetch_download_url(client, headers_field, user_hash, download_id)

            dest_dir = _track_dest_dir(resolve_destination_root(db), track)
            dest_dir.mkdir(parents=True, exist_ok=True)
            filename = sanitize_dir_name(track.title or download_id) + _guess_extension(url)
            dest_path = dest_dir / filename

            async with client.stream("GET", url) as resp:
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
        # downloads/worker.py and audible/pdf_downloader.py already use.
        if dest_path.exists() and dest_path.stat().st_size > 0:
            job.status = STATUS_COMPLETED
            job.downloaded_path = str(dest_path)
        else:
            job.status = STATUS_FAILED
            job.error_message = "Downloaded file is missing or empty."
        job.completed_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
