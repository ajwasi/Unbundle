import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.downloads import paths, worker
from app.models.download import STATUS_COMPLETED, STATUS_FAILED, Download
from app.models.download_job import STATUS_COMPLETED as JOB_COMPLETED, STATUS_FAILED as JOB_FAILED, STATUS_RUNNING, DownloadJob
from tests.factories import make_order, make_subproduct


def _seed_bundle(make_bundle, gamekey="GK1", item_name="Cool Book", filename="book.epub", size=1234):
    order = make_order(
        name="Test Bundle",
        subproducts=[
            make_subproduct(
                human_name=item_name,
                downloads=[
                    {"download_struct": [{"name": "EPUB", "file_size": size, "url": {"web": f"https://dl.humble.com/{filename}"}}]}
                ],
            )
        ],
    )
    return make_bundle(gamekey=gamekey, order=order)


def _predicted_path(bundle_name, item_name, filename):
    return settings.downloads_dir / paths.predict_download_path(bundle_name, item_name, filename)


@pytest.mark.asyncio
async def test_run_job_marks_completed_when_file_exists_and_nonempty(db, make_bundle):
    bundle = _seed_bundle(make_bundle)
    target = _predicted_path(bundle.name, "Cool Book", "book.epub")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * 999)  # deliberately NOT matching the metadata size of 1234

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    db.refresh(job)
    assert job.status == JOB_COMPLETED

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.status == STATUS_COMPLETED
    # Ground truth is the actual on-disk size, not Humble's (possibly stale) metadata.
    assert row.expected_size_bytes == 999


@pytest.mark.asyncio
async def test_run_job_marks_failed_when_file_missing(db, make_bundle):
    bundle = _seed_bundle(make_bundle)
    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    db.refresh(job)
    assert job.status == JOB_FAILED

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.status == STATUS_FAILED


@pytest.mark.asyncio
async def test_run_job_marks_failed_when_file_exists_but_empty(db, make_bundle):
    bundle = _seed_bundle(make_bundle)
    target = _predicted_path(bundle.name, "Cool Book", "book.epub")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.status == STATUS_FAILED


@pytest.mark.asyncio
async def test_run_job_fails_when_cli_exits_nonzero_even_if_file_present(db, make_bundle):
    bundle = _seed_bundle(make_bundle)
    target = _predicted_path(bundle.name, "Cool Book", "book.epub")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * 10)

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(1, "some cli error"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    db.refresh(job)
    assert job.status == JOB_FAILED
    assert "exited with code 1" in job.error_message


@pytest.mark.asyncio
async def test_run_job_marks_failed_without_touching_filesystem_when_name_is_dotdot(db, make_bundle, tmp_path):
    # sanitize_dir_name() strips path separators but not "..", which contains
    # none — a bundle name of exactly ".." (only plausible if Humble's own data
    # contained it) must not let the verification step climb out of downloads_dir.
    order = make_order(
        name="..",
        subproducts=[
            make_subproduct(
                human_name="Cool Book",
                downloads=[{"download_struct": [{"name": "EPUB", "file_size": 10, "url": {"web": "https://dl.humble.com/book.epub"}}]}],
            )
        ],
    )
    bundle = make_bundle(gamekey="GK1", order=order)

    # A real file one level above downloads_dir, at the path the escape would target.
    escape_target = settings.downloads_dir.parent / "Cool Book" / "book.epub"

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    db.refresh(job)
    assert job.status == JOB_FAILED
    assert not escape_target.exists()

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.status == STATUS_FAILED
    assert "outside downloads root" in row.error_message


@pytest.mark.asyncio
async def test_run_job_only_verifies_requested_indices(db, make_bundle):
    order = make_order(
        subproducts=[
            make_subproduct(human_name="Item One", downloads=[{"download_struct": [{"name": "EPUB", "file_size": 10, "url": {"web": "https://dl.humble.com/one.epub"}}]}]),
            make_subproduct(human_name="Item Two", downloads=[{"download_struct": [{"name": "EPUB", "file_size": 10, "url": {"web": "https://dl.humble.com/two.epub"}}]}]),
        ]
    )
    bundle = make_bundle(gamekey="GK2", order=order)
    # Only create the file for item 1 — item 2 was never requested, so it must
    # not be checked (and therefore not marked failed).
    target = _predicted_path(bundle.name, "Item One", "one.epub")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * 10)

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name, requested_indices="1")
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, [1], None)

    db.refresh(job)
    assert job.status == JOB_COMPLETED
    rows = db.query(Download).filter(Download.gamekey == bundle.gamekey).all()
    assert len(rows) == 1
    assert rows[0].item_name == "Item One"


@pytest.mark.asyncio
async def test_run_job_unexpected_exception_still_marks_job_failed(db, make_bundle):
    bundle = _seed_bundle(make_bundle)
    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(side_effect=RuntimeError("boom"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    db.refresh(job)
    assert job.status == JOB_FAILED
    assert "boom" in job.error_message


@pytest.mark.asyncio
async def test_start_download_raises_when_already_running(db, make_bundle):
    bundle = _seed_bundle(make_bundle)
    async with worker._download_lock:
        with pytest.raises(RuntimeError):
            await worker.start_download(bundle.gamekey, bundle.name, None, None)


@pytest.mark.asyncio
async def test_start_download_creates_a_queued_job_row(db, make_bundle):
    bundle = _seed_bundle(make_bundle)
    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        job_id = await worker.start_download(bundle.gamekey, bundle.name, [1], ["EPUB"])
        # Let the scheduled background task actually run to completion before asserting.
        await asyncio.sleep(0.05)

    job = db.get(DownloadJob, job_id)
    assert job.requested_indices == "1"
    assert job.requested_formats == "EPUB"


def test_sweep_stale_jobs_marks_running_as_failed(db):
    job = DownloadJob(gamekey="GK1", status=STATUS_RUNNING)
    db.add(job)
    db.commit()
    db.refresh(job)

    worker.sweep_stale_jobs()

    db.refresh(job)
    assert job.status == JOB_FAILED
    assert "restart" in job.error_message.lower()


def test_latest_job_for_bundle_returns_most_recent(db):
    j1 = DownloadJob(gamekey="GK1")
    db.add(j1)
    db.commit()
    j2 = DownloadJob(gamekey="GK1")
    db.add(j2)
    db.commit()

    latest = worker.latest_job_for_bundle(db, "GK1")
    assert latest.id == j2.id
