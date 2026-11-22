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
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.humble_connector import parse_bundle
from app import jobqueue
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
from app.timeutil import utcnow

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


def _current_concurrency_limit(db: Session) -> int:
    row = db.get(DownloadSettings, 1)
    return row.concurrency if row is not None else settings.download_concurrency


def _parse_indices(raw: str) -> list[int] | None:
    return [int(x) for x in raw.split(",")] if raw else None


def _parse_formats(raw: str) -> list[str] | None:
    return raw.split(",") if raw else None


def _is_on_disk(row: Download | None) -> bool:
    """True when this item already has a completed row whose file is still
    where that row says. Trusts the filesystem over a possibly-stale
    completed status — a file deleted or moved away since is fetched again.
    This is what lets a file already relocated to a destination folder, or
    marked downloaded by a folder scan, count as done: humble-cli itself only
    ever looks at the staging path, so without this it would download both
    again.
    """
    if row is None or row.status != FILE_COMPLETED or not row.current_location_path:
        return False
    path = paths.long_path_safe(Path(row.current_location_path))
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


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
    leave a download_job stuck in 'running' forever (see jobqueue.sweep_stale).
    """
    jobqueue.sweep_stale(DownloadJob, running=JOB_RUNNING, failed=JOB_FAILED, finished_attr="finished_at")


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
        jobqueue.dispatch_queued(
            db,
            DownloadJob,
            queued=JOB_QUEUED,
            running=JOB_RUNNING,
            running_ids=_running_job_ids,
            limit=_current_concurrency_limit(db),
            make_task=lambda _db, job: _run_job(
                job.id, job.gamekey, _parse_indices(job.requested_indices), _parse_formats(job.requested_formats)
            ),
        )
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
    last_time = utcnow()
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
            now = utcnow()
            elapsed = (now - last_time).total_seconds()
            bytes_per_sec = max(0.0, (bytes_done - last_bytes) / elapsed) if elapsed > 0 else 0.0
            _progress[job_id] = {
                "bytes_done": min(bytes_done, bytes_total) if bytes_total else bytes_done,
                "bytes_total": bytes_total,
                "bytes_per_sec": bytes_per_sec,
            }
            last_bytes, last_time = bytes_done, now
    except asyncio.CancelledError:
        # Normal shutdown path: nothing to clean up, but cancellation must propagate.
        raise


def _download_row(db: Session, *, gamekey: str, job_id: int, bundle_name: str, item) -> Download:
    """The Download row for one expected file, created if this is its first
    attempt, with the fields that describe *this* job refreshed.

    One query per item rather than a bulk prefetch (contrast sync/refresh.py)
    — deliberately left this way: an expected list is bounded by one bundle's
    own subproduct count (single digits to low dozens), and this runs once per
    file inside a job already dominated by real download I/O (seconds to
    minutes), not by these sub-millisecond, already-indexed lookups. Worth
    bulk-fetching if this ever processes whole-library-sized batches like
    refresh_library does; not worth the extra state for what it does today.
    """
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
        row = Download(gamekey=gamekey, item_name=item.item_name, original_filename=item.original_filename)
        db.add(row)
    row.download_job_id = job_id
    row.bundle_name = bundle_name
    row.subproduct_index = item.subproduct_index
    row.file_format = item.file_format
    row.source_url = item.source_url
    return row


def _plan_run(db: Session, gamekey: str, indices: list[int] | None, formats: list[str] | None, downloads: list):
    """(todo, run_indices): the files this job still has to fetch, and the
    subproduct indices to hand humble-cli.

    Items already on disk (see _is_on_disk) are left out of the run entirely:
    not handed to humble-cli, and not re-verified afterwards — their existing
    completed rows already say where they are. When some were left out the
    indices are narrowed to just what remains; otherwise the caller's own
    selection is passed through untouched.
    """
    expected = [
        item
        for item in downloads
        if (not indices or item.subproduct_index in indices) and (not formats or item.file_format in formats)
    ]
    completed = {
        (d.item_name, d.original_filename): d
        for d in db.query(Download).filter(Download.gamekey == gamekey, Download.status == FILE_COMPLETED)
    }
    todo = [item for item in expected if not _is_on_disk(completed.get((item.item_name, item.original_filename)))]
    run_indices = sorted({item.subproduct_index for item in todo}) if len(todo) != len(expected) else indices
    return todo, run_indices


async def _run_humble_cli(
    job_id: int, gamekey: str, bundle_name: str, todo: list, run_indices, formats, downloads_root
) -> tuple[int, str]:
    """(returncode, output). Nothing to fetch is a clean success, without
    spawning the process. While it runs, a sibling task polls the predicted
    file paths for progress (see _poll_progress) and is cancelled afterwards."""
    if not todo:
        return 0, ""
    poll_task = asyncio.create_task(_poll_progress(job_id, bundle_name, todo, downloads_root))
    try:
        return await runner.run_download_job(settings.humble_cli_path, gamekey, run_indices, formats, downloads_root)
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task


def _verify_item(db: Session, *, gamekey: str, job_id: int, bundle_name: str, item, downloads_root) -> bool:
    """Checks that one expected file really landed, and records the result on
    its Download row (relocating it on first success). True when it's fine."""
    rel_path = paths.predict_download_path(bundle_name, item.item_name, item.original_filename)
    try:
        abs_path = paths.resolve_within(downloads_root, rel_path)
    except paths.PathTraversalError as exc:
        row = _download_row(db, gamekey=gamekey, job_id=job_id, bundle_name=bundle_name, item=item)
        row.status = FILE_FAILED
        row.error_message = str(exc)
        db.flush()
        return False

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

    row = _download_row(db, gamekey=gamekey, job_id=job_id, bundle_name=bundle_name, item=item)
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
            dest_dir = relocate.resolve_destination(db, gamekey, item.machine_name, item.file_format)
            if dest_dir is not None:
                try:
                    final_path = relocate.relocate(check_path, dest_dir)
                except OSError as exc:
                    row.error_message = f"Downloaded but failed to relocate: {exc}"
            row.current_location_path = str(final_path)
        row.completed_at = utcnow()
    else:
        row.status = FILE_FAILED
        row.error_message = "File not found or wrong size after download attempt."
    db.flush()
    return ok


def _job_outcome(returncode: int, output: str, any_failed: bool) -> tuple[str, str]:
    """(status, error_message) for the job as a whole; message is empty on success."""
    if returncode != 0:
        return JOB_FAILED, f"humble-cli exited with code {returncode}. Output tail: {output[-500:]}"
    if any_failed:
        return JOB_FAILED, "One or more files did not verify after download — see item statuses below."
    return JOB_COMPLETED, ""


async def _run_job(job_id: int, gamekey: str, indices: list[int] | None, formats: list[str] | None) -> None:
    db = SessionLocal()
    try:
        job = db.get(DownloadJob, job_id)

        try:
            downloads_root = settings.downloads_dir
            bundle = db.get(Bundle, gamekey)
            normalized = parse_bundle(gamekey, json.loads(bundle.raw_json))
            todo, run_indices = _plan_run(db, gamekey, indices, formats, normalized.downloads)

            returncode, output = await _run_humble_cli(
                job_id, gamekey, bundle.name, todo, run_indices, formats, downloads_root
            )

            any_failed = False
            for item in todo:
                if not _verify_item(
                    db, gamekey=gamekey, job_id=job_id, bundle_name=bundle.name, item=item, downloads_root=downloads_root
                ):
                    any_failed = True

            job.status, message = _job_outcome(returncode, output, any_failed)
            if message:
                job.error_message = message
        except Exception as exc:  # noqa: BLE001 - last-resort guard so download_job never hangs at 'running'
            job.status = JOB_FAILED
            job.error_message = f"Unexpected error: {exc}"

        job.finished_at = utcnow()
        db.commit()
    finally:
        _running_job_ids.discard(job_id)
        _progress.pop(job_id, None)
        db.close()
        await try_dispatch_queued_downloads()
