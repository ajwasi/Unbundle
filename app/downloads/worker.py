"""Runs one DownloadJob: shells out to the real humble-cli binary (runner.py),
then verifies results by checking the filesystem against predicted paths
(paths.py) — never by parsing its stdout. Mirrors sync/refresh.py's
background-task-with-a-lock pattern exactly, including the reason for it:
a real subprocess run is slow enough (and, per the plan's own open risk list,
concurrent humble-cli invocations against one account are unverified) that it
must not block the request, and only one should run at a time.
"""

import asyncio
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
    STATUS_RUNNING as JOB_RUNNING,
    DownloadJob,
)

_download_lock = asyncio.Lock()


def is_download_running() -> bool:
    return _download_lock.locked()


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
    if _download_lock.locked():
        raise RuntimeError("A download is already in progress.")

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

    asyncio.create_task(_run_job(job_id, gamekey, indices, formats))
    return job_id


async def _run_job(job_id: int, gamekey: str, indices: list[int] | None, formats: list[str] | None) -> None:
    async with _download_lock:
        db = SessionLocal()
        try:
            job = db.get(DownloadJob, job_id)
            job.status = JOB_RUNNING
            job.started_at = datetime.utcnow()
            db.commit()

            try:
                downloads_root = settings.downloads_dir
                returncode, output = await runner.run_download_job(
                    settings.humble_cli_path, gamekey, indices, formats, downloads_root
                )

                bundle = db.get(Bundle, gamekey)
                normalized = parse_bundle(gamekey, json.loads(bundle.raw_json))
                expected = [
                    item
                    for item in normalized.downloads
                    if (not indices or item.subproduct_index in indices)
                    and (not formats or item.file_format in formats)
                ]

                def _row_for(item) -> Download:
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
            db.close()
