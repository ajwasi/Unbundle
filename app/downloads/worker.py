"""Runs DownloadJobs from a real queue: any number can be "started" (queued),
but only up to the configured concurrency limit (DownloadSettings, falling
back to settings.download_concurrency) actually run at once — the rest wait
as STATUS_QUEUED until a running one finishes. Verifies results by checking
the filesystem against predicted paths (paths.py) — never by parsing
humble-cli's stdout, including for the live progress/speed numbers (see
_poll_progress), which come from repeatedly stat()-ing the same predicted
paths the final verification step already trusts.

_running_job_ids + _progress are plain module-level state, not persisted —
this app is explicitly single-process (see main.py's own "never add
--workers" comment), so that's sufficient and avoids writing to the database
on every ~1.5s progress tick for numbers that are only ever meant to be live.
"""

import asyncio
import contextlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.humble_connector import parse_bundle
from app.db import SessionLocal
from app.downloads import paths, relocate, runner
from app.models.bundle import Bundle
from app.models.download import STATUS_COMPLETED as FILE_COMPLETED, STATUS_FAILED as FILE_FAILED, Download
from app.models.download_job import (
    STATUS_COMPLETED as JOB_COMPLETED,
    STATUS_FAILED as JOB_FAILED,
    STATUS_QUEUED as JOB_QUEUED,
    STATUS_RUNNING as JOB_RUNNING,
    DownloadJob,
)
from app.models.download_settings import DownloadSettings

# Job ids currently executing (spawned as asyncio tasks) — bounded by the
# configured concurrency limit; everything else waits in the DB as
# STATUS_QUEUED. Not a Semaphore on purpose: the limit needs to be re-read
# live (Settings-page editable, no restart), and a Semaphore's own capacity
# can't be changed after construction.
_running_job_ids: set[int] = set()
# job_id -> {"bytes_done", "bytes_total", "bytes_per_sec"} for whatever's
# currently running — see _poll_progress. Absent entry == not running yet
# (still queued) or already finished.
_progress: dict[int, dict] = {}
# Holds a strong reference to every spawned _run_job task. asyncio.create_task()
# with its return value stored nowhere is eligible for garbage collection as
# soon as nothing else references it — a documented asyncio hazard (see
# create_task's own docs), not a hypothetical one: confirmed as the real cause
# of an intermittent test failure (tests/test_downloads_worker.py) where a
# spawned task got collected mid-flight under a busy full-suite run, before
# its own `finally: _running_job_ids.discard(...)` had a chance to run,
# leaving that id stuck there for whatever test ran next. Entries remove
# themselves via a done-callback once finished.
_background_tasks: set[asyncio.Task] = set()


def _current_concurrency_limit(db: Session) -> int:
    row = db.get(DownloadSettings, 1)
    return row.concurrency if row is not None else settings.download_concurrency


def _parse_indices(raw: str) -> list[int] | None:
    return [int(x) for x in raw.split(",")] if raw else None


def _parse_formats(raw: str) -> list[str] | None:
    return raw.split(",") if raw else None


def get_active_jobs(db: Session) -> list[dict]:
    """Every running or queued job, for the Downloads page's Active section
    and the bundle-detail page's own per-bundle status. Running jobs carry
    live progress/speed from _progress; queued ones just don't have an entry
    there yet.
    """
    jobs = (
        db.query(DownloadJob)
        .filter(DownloadJob.status.in_((JOB_RUNNING, JOB_QUEUED)))
        .order_by(DownloadJob.id)
        .all()
    )
    rows = []
    for job in jobs:
        progress = _progress.get(job.id, {})
        rows.append(
            {
                "id": job.id,
                "gamekey": job.gamekey,
                "bundle_name": job.bundle_name,
                "status": job.status,
                "bytes_done": progress.get("bytes_done", 0),
                "bytes_total": progress.get("bytes_total", 0),
                "bytes_per_sec": progress.get("bytes_per_sec", 0.0),
            }
        )
    return rows


def get_job_progress(job_id: int) -> dict:
    """Just the live numbers for one already-known job (the caller already has
    the DownloadJob row itself, e.g. via latest_job_for_bundle) — a queued job
    simply has no entry yet, same as in get_active_jobs.
    """
    progress = _progress.get(job_id, {})
    return {
        "bytes_done": progress.get("bytes_done", 0),
        "bytes_total": progress.get("bytes_total", 0),
        "bytes_per_sec": progress.get("bytes_per_sec", 0.0),
    }


def is_download_running() -> bool:
    return len(_running_job_ids) > 0


def sweep_stale_jobs() -> None:
    """Call on app startup — a container restart mid-download would otherwise
    leave a download_job stuck in 'running' forever (same reasoning as
    refresh.sweep_stale_runs).
    """
    db = SessionLocal()
    try:
        stale = db.query(DownloadJob).filter(DownloadJob.status == JOB_RUNNING).all()
        for job in stale:
            job.status = JOB_FAILED
            job.finished_at = datetime.utcnow()
            job.error_message = "Interrupted by application restart."
        db.commit()
    finally:
        db.close()


def latest_job_for_bundle(db: Session, gamekey: str) -> DownloadJob | None:
    return (
        db.query(DownloadJob)
        .filter(DownloadJob.gamekey == gamekey)
        .order_by(DownloadJob.id.desc())
        .first()
    )


async def start_download(
    gamekey: str, bundle_name: str, indices: list[int] | None, formats: list[str] | None = None
) -> int:
    """Always queues — never raises for "already busy" anymore. Whether this
    job starts immediately or waits behind others is entirely
    try_dispatch_queued_downloads's call, made right after."""
    db = SessionLocal()
    try:
        job = DownloadJob(
            gamekey=gamekey,
            bundle_name=bundle_name,
            requested_indices=",".join(str(i) for i in sorted(set(indices))) if indices else "",
            requested_formats=",".join(sorted(set(formats))) if formats else "",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    await try_dispatch_queued_downloads()
    return job_id


async def try_dispatch_queued_downloads() -> None:
    """Starts as many queued jobs as the current concurrency limit allows.
    Called after a job is queued and again after one finishes — no separate
    polling loop needed. The limit is re-read from the database every call
    (not cached), so a Settings-page change takes effect on the very next
    dispatch decision, no restart.

    Marks a claimed job JOB_RUNNING (and commits) before spawning its task —
    not inside _run_job — specifically so the *next* loop iteration's "give
    me the oldest still-queued job" query doesn't hand back the same row
    again. There's no `await` between checking len(_running_job_ids) and
    adding to it, so two calls landing back-to-back (a job queuing right as
    another finishes) can't both claim the same slot — this whole function
    only ever does synchronous DB calls until it spawns a task.
    """
    db = SessionLocal()
    try:
        limit = _current_concurrency_limit(db)
        while len(_running_job_ids) < limit:
            next_job = db.query(DownloadJob).filter(DownloadJob.status == JOB_QUEUED).order_by(DownloadJob.id).first()
            if next_job is None:
                break
            next_job.status = JOB_RUNNING
            next_job.started_at = datetime.utcnow()
            db.commit()
            indices = _parse_indices(next_job.requested_indices)
            formats = _parse_formats(next_job.requested_formats)
            _running_job_ids.add(next_job.id)
            task = asyncio.create_task(_run_job(next_job.id, next_job.gamekey, indices, formats))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
    finally:
        db.close()


async def _poll_progress(job_id: int, bundle_name: str, expected: list, downloads_root) -> None:
    """Runs alongside runner.run_download_job() for exactly one job (spawned
    and cancelled around that one call, in _run_job) — repeatedly stat()-ing
    the same predicted paths the final verification step already trusts,
    rather than parsing any subprocess output. bytes_total is Humble's own
    expected_size_bytes metadata, already established elsewhere in this file
    as occasionally stale — fine here since it only makes a progress
    *percentage* imprecise, never the pass/fail verification itself.
    """
    bytes_total = sum(item.expected_size_bytes for item in expected)
    last_bytes = 0
    last_time = datetime.utcnow()
    try:
        while True:
            await asyncio.sleep(1.5)
            bytes_done = 0
            for item in expected:
                try:
                    rel_path = paths.predict_download_path(bundle_name, item.item_name, item.original_filename)
                    abs_path = paths.resolve_within(downloads_root, rel_path)
                except paths.PathTraversalError:
                    continue
                check_path = paths.long_path_safe(abs_path)
                if check_path.is_file():
                    bytes_done += check_path.stat().st_size
            now = datetime.utcnow()
            elapsed = (now - last_time).total_seconds()
            bytes_per_sec = max(0.0, (bytes_done - last_bytes) / elapsed) if elapsed > 0 else 0.0
            _progress[job_id] = {
                "bytes_done": min(bytes_done, bytes_total) if bytes_total else bytes_done,
                "bytes_total": bytes_total,
                "bytes_per_sec": bytes_per_sec,
            }
            last_bytes, last_time = bytes_done, now
    except asyncio.CancelledError:
        pass


async def _run_job(job_id: int, gamekey: str, indices: list[int] | None, formats: list[str] | None) -> None:
    db = SessionLocal()
    try:
        job = db.get(DownloadJob, job_id)

        try:
            downloads_root = settings.downloads_dir
            bundle = db.get(Bundle, gamekey)
            normalized = parse_bundle(gamekey, json.loads(bundle.raw_json))
            expected = [
                item
                for item in normalized.downloads
                if (not indices or item.subproduct_index in indices)
                and (not formats or item.file_format in formats)
            ]

            poll_task = asyncio.create_task(_poll_progress(job_id, bundle.name, expected, downloads_root))
            try:
                returncode, output = await runner.run_download_job(
                    settings.humble_cli_path, gamekey, indices, formats, downloads_root
                )
            finally:
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll_task

            def _row_for(item) -> Download:
                # One query per item rather than a bulk prefetch (contrast
                # sync/refresh.py) — deliberately left this way: `expected` is
                # bounded by one bundle's own subproduct count (single digits to
                # low dozens), and this whole function runs once per completed
                # file inside a job that's already dominated by real download
                # I/O (seconds to minutes), not by these sub-millisecond,
                # already-indexed lookups. Worth bulk-fetching if this ever
                # processes whole-library-sized batches like refresh_library
                # does; not worth the extra state for what it does today.
                row = (
                    db.query(Download)
                    .filter(
                        Download.gamekey == gamekey,
                        Download.item_name == item.item_name,
                        Download.original_filename == item.original_filename,
                    )
                    .one_or_none()
                )
                if row is None:
                    row = Download(
                        gamekey=gamekey,
                        item_name=item.item_name,
                        original_filename=item.original_filename,
                    )
                    db.add(row)
                row.download_job_id = job_id
                row.bundle_name = bundle.name
                row.subproduct_index = item.subproduct_index
                row.file_format = item.file_format
                row.source_url = item.source_url
                return row

            any_failed = False
            for item in expected:
                rel_path = paths.predict_download_path(bundle.name, item.item_name, item.original_filename)
                try:
                    abs_path = paths.resolve_within(downloads_root, rel_path)
                except paths.PathTraversalError as exc:
                    row = _row_for(item)
                    row.status = FILE_FAILED
                    row.error_message = str(exc)
                    db.flush()
                    any_failed = True
                    continue

                check_path = paths.long_path_safe(abs_path)
                exists = check_path.is_file()
                actual_size = check_path.stat().st_size if exists else 0
                # Not an exact match against item.expected_size_bytes: confirmed against a real
                # account (2026-09-06) that Humble's own API-reported file_size can be stale
                # relative to the file actually served (observed both directions — one book's
                # real file was ~5x larger than its metadata, another ~20% smaller). humble-cli
                # itself never validates size after a fresh download either, only uses it to
                # decide whether to skip re-fetching an already-present file. Non-empty + present
                # is the honest success signal; exact-match produced false "failed" verdicts on
                # genuinely-successful downloads.
                ok = exists and actual_size > 0

                row = _row_for(item)
                # Ground truth (the file actually on disk) once we have it, not Humble's
                # metadata value — see the "ok" comment above on why they can disagree.
                row.expected_size_bytes = actual_size if ok else item.expected_size_bytes
                if ok:
                    row.status = FILE_COMPLETED
                    row.error_message = ""
                    row.progress_bytes = actual_size
                    row.original_download_path = row.original_download_path or str(rel_path)
                    row.current_location_type = row.current_location_type or "local"
                    # Only attempt relocation the first time this row completes — on a
                    # re-verify of an already-completed row, current_location_path is
                    # already set (and the file already moved away from check_path),
                    # so re-running this would just fail to find it at the old spot.
                    if not row.current_location_path:
                        final_path = abs_path
                        # humble-cli names the file straight from its download URL, which
                        # is what check_path/abs_path point at here — humanize it (pure
                        # underscore->space swap, see paths.humanize_filename) before any
                        # configured relocation, so the filename a human actually sees is
                        # legible. Best-effort: a rename failure (e.g. a same-named file
                        # already sitting there) just leaves the original name in place
                        # rather than failing an otherwise-successful download.
                        humanized_name = paths.humanize_filename(abs_path.name)
                        if humanized_name != abs_path.name:
                            candidate = abs_path.with_name(humanized_name)
                            try:
                                check_path.rename(paths.long_path_safe(candidate))
                                final_path = candidate
                                check_path = paths.long_path_safe(candidate)
                            except OSError:
                                pass
                        dest_dir = relocate.resolve_destination(
                            db, gamekey, item.machine_name, item.file_format
                        )
                        if dest_dir is not None:
                            try:
                                final_path = relocate.relocate(check_path, dest_dir)
                            except OSError as exc:
                                row.error_message = f"Downloaded but failed to relocate: {exc}"
                        row.current_location_path = str(final_path)
                    row.completed_at = datetime.utcnow()
                else:
                    any_failed = True
                    row.status = FILE_FAILED
                    row.error_message = "File not found or wrong size after download attempt."
                db.flush()

            if returncode != 0:
                job.status = JOB_FAILED
                job.error_message = f"humble-cli exited with code {returncode}. Output tail: {output[-500:]}"
            elif any_failed:
                job.status = JOB_FAILED
                job.error_message = "One or more files did not verify after download — see item statuses below."
            else:
                job.status = JOB_COMPLETED
        except Exception as exc:  # noqa: BLE001 - last-resort guard so download_job never hangs at 'running'
            job.status = JOB_FAILED
            job.error_message = f"Unexpected error: {exc}"

        job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        _running_job_ids.discard(job_id)
        _progress.pop(job_id, None)
        db.close()
        await try_dispatch_queued_downloads()
