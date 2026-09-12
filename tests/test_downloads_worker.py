import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.downloads import paths, worker
from app.models.download import STATUS_COMPLETED, STATUS_FAILED, Download
from app.models.download_job import (
    STATUS_COMPLETED as JOB_COMPLETED,
    STATUS_FAILED as JOB_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    DownloadJob,
)
from app.models.download_settings import DownloadSettings
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
async def test_run_job_renames_an_underscore_filename_to_a_readable_one(db, make_bundle):
    bundle = _seed_bundle(make_bundle, filename="some_book_vol_02.epub", size=999)
    target = _predicted_path(bundle.name, "Cool Book", "some_book_vol_02.epub")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * 999)

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    renamed = target.with_name("some book vol 02.epub")
    assert renamed.is_file()
    assert not target.exists()

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.status == STATUS_COMPLETED
    assert row.current_location_path == str(renamed)
    # original_download_path still records the raw, as-downloaded name —
    # only the file's own resting name (and current_location_path) change.
    assert "some_book_vol_02.epub" in row.original_download_path


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
async def test_start_download_never_raises_it_queues_instead(db, make_bundle):
    # A second (or third...) download attempt while one is already running
    # used to be rejected outright (RuntimeError) — it's queued now.
    bundles = [_seed_bundle(make_bundle, gamekey=f"GK{i}", filename=f"book{i}.epub") for i in range(3)]
    for i, b in enumerate(bundles):
        target = _predicted_path(b.name, "Cool Book", f"book{i}.epub")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 10)
    release = asyncio.Event()

    async def _blocked_run(*args, **kwargs):
        await release.wait()
        return (0, "ok")

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(side_effect=_blocked_run)):
        for b in bundles:
            job_id = await worker.start_download(b.gamekey, b.name, [1], ["EPUB"])
            assert job_id is not None  # never raises
        await asyncio.sleep(0.05)  # let the dispatcher's spawned tasks actually start

        statuses = sorted(j.status for j in db.query(DownloadJob).all())
        # concurrency defaults to settings.download_concurrency (2, no
        # DownloadSettings row yet) — exactly 2 running, 1 waiting.
        assert statuses == sorted([STATUS_QUEUED, STATUS_RUNNING, STATUS_RUNNING])

        release.set()
        await asyncio.sleep(0.1)  # let all three finish, including the one that was queued

    db.expire_all()
    assert {j.status for j in db.query(DownloadJob).all()} == {JOB_COMPLETED}


@pytest.mark.asyncio
async def test_concurrency_setting_controls_how_many_jobs_run_at_once(db, make_bundle):
    db.add(DownloadSettings(id=1, concurrency=1))
    db.commit()
    bundles = [_seed_bundle(make_bundle, gamekey=f"GK{i}", filename=f"book{i}.epub") for i in range(2)]
    release = asyncio.Event()

    async def _blocked_run(*args, **kwargs):
        await release.wait()
        return (0, "ok")

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(side_effect=_blocked_run)):
        for b in bundles:
            await worker.start_download(b.gamekey, b.name, [1], ["EPUB"])
        await asyncio.sleep(0.05)

        statuses = sorted(j.status for j in db.query(DownloadJob).all())
        assert statuses == sorted([STATUS_QUEUED, STATUS_RUNNING])

        release.set()
        await asyncio.sleep(0.1)


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


@pytest.mark.asyncio
async def test_spawned_download_tasks_are_tracked_until_they_finish(db, make_bundle):
    # Regression guard for the actual cause of an intermittent failure in
    # test_start_download_never_raises_it_queues_instead: asyncio.create_task()
    # with its return value stored nowhere is eligible for garbage collection
    # as soon as nothing references it — confirmed as the real mechanism,
    # under a busy full-suite run, for a spawned _run_job task getting
    # collected before its own `finally: _running_job_ids.discard(...)` ran,
    # leaving that id stuck for whatever test happened to run next. This locks
    # in that a reference is held in _background_tasks while a job runs and
    # released (via task.add_done_callback) once it finishes — not just that
    # the job completes, which the test above already covers.
    bundle = _seed_bundle(make_bundle)
    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker.start_download(bundle.gamekey, bundle.name, [1], ["EPUB"])
        assert len(worker._background_tasks) == 1

        for _ in range(100):  # up to 2s, polled rather than a fixed guess
            if not worker._background_tasks:
                break
            await asyncio.sleep(0.02)
        assert worker._background_tasks == set()


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


@pytest.mark.asyncio
async def test_progress_polling_tracks_bytes_done_and_speed_while_running(db, make_bundle):
    # Progress comes from repeatedly stat()-ing the predicted output path
    # while the (mocked) subprocess is "running" — never from parsing any
    # output — so this drives that by writing real bytes to the real
    # predicted path partway through a long-running mocked call.
    bundle = _seed_bundle(make_bundle, size=1000)
    target = _predicted_path(bundle.name, "Cool Book", "book.epub")
    target.parent.mkdir(parents=True, exist_ok=True)

    async def _slow_run(*args, **kwargs):
        await asyncio.sleep(0.1)
        target.write_bytes(b"x" * 500)
        await asyncio.sleep(3.0)  # wide margin around _poll_progress's ~1.5s interval
        target.write_bytes(b"x" * 1000)
        return (0, "ok")

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(side_effect=_slow_run)):
        job_id = await worker.start_download(bundle.gamekey, bundle.name, [1], ["EPUB"])

        await asyncio.sleep(2.0)  # past the poller's first check, well before _slow_run returns
        mid_progress = worker.get_job_progress(job_id)
        assert mid_progress["bytes_done"] == 500
        assert mid_progress["bytes_total"] == 1000
        assert mid_progress["bytes_per_sec"] > 0

        await asyncio.sleep(1.5)  # let the job finish

    db.expire_all()
    job = db.get(DownloadJob, job_id)
    assert job.status == JOB_COMPLETED
    # Cleared once the job finishes — not left showing stale "still running" numbers.
    assert worker.get_job_progress(job_id) == {"bytes_done": 0, "bytes_total": 0, "bytes_per_sec": 0.0}
