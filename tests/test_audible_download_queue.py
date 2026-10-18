import asyncio
import time
from datetime import datetime

import pytest

from app.audible import book_downloader
from app.audible import scan as audible_scan
from app.db import SessionLocal
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AudibleBookDownload,
)
from app.models.audible_local_file import SOURCE_SCAN, AudibleLocalFile
from app.models.credential import SOURCE_AUDIBLE, STATUS_OK, Credential
from app.models.download_settings import DownloadSettings
from app.security import encrypt_json


async def _await_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _add_books(db, *titles):
    for i, title in enumerate(titles, start=1):
        db.add(AudibleBook(asin=f"B00{i}", title=title, author="An Author"))
    db.commit()


@pytest.fixture(autouse=True)
def _clean_queue_state(monkeypatch):
    monkeypatch.setattr(book_downloader, "_running_job_ids", set())
    monkeypatch.setattr(book_downloader, "_progress", {})


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    return tmp_path


def _fake_run(finish_with=STATUS_COMPLETED, delay=0.05, seen=None):
    """Stands in for the real license/download/ffmpeg pipeline. Honors the same
    bookkeeping contract _run does (free the slot, then dispatch the next)."""

    async def fake(job_id, asin, title):
        db = SessionLocal()
        try:
            if seen is not None:
                seen["max"] = max(seen["max"], len(book_downloader._running_job_ids))
            await asyncio.sleep(delay)
            job = db.get(AudibleBookDownload, job_id)
            job.status = finish_with
            job.completed_at = datetime.utcnow()
            db.commit()
        finally:
            book_downloader._running_job_ids.discard(job_id)
            db.close()
            book_downloader.try_dispatch_queued_downloads()

    return fake


# ------------------------------------------------------------------ queueing


@pytest.mark.asyncio
async def test_queue_many_queues_every_book_and_runs_them_all(db, downloads_dir, monkeypatch):
    _add_books(db, "One", "Two", "Three")
    monkeypatch.setattr(book_downloader, "_run", _fake_run())

    outcome = book_downloader.queue_many(db, ["B001", "B002", "B003"])

    assert outcome["queued"] == ["B001", "B002", "B003"]
    assert await _await_until(
        lambda: db.query(AudibleBookDownload).filter(AudibleBookDownload.status == STATUS_COMPLETED).count() == 3
    )


@pytest.mark.asyncio
async def test_only_as_many_run_at_once_as_the_concurrency_limit_allows(db, downloads_dir, monkeypatch):
    db.add(DownloadSettings(id=1, concurrency=2))
    db.commit()
    _add_books(db, "One", "Two", "Three", "Four")
    seen = {"max": 0}
    monkeypatch.setattr(book_downloader, "_run", _fake_run(seen=seen))

    book_downloader.queue_many(db, ["B001", "B002", "B003", "B004"])

    # Right after queueing, two are claimed and the rest wait.
    db.expire_all()
    assert db.query(AudibleBookDownload).filter(AudibleBookDownload.status == STATUS_RUNNING).count() == 2
    assert db.query(AudibleBookDownload).filter(AudibleBookDownload.status == STATUS_QUEUED).count() == 2
    assert await _await_until(
        lambda: (db.expire_all() or db.query(AudibleBookDownload).filter(AudibleBookDownload.status == STATUS_COMPLETED).count()) == 4
    )
    assert seen["max"] == 2


@pytest.mark.asyncio
async def test_queue_many_reports_why_a_book_was_not_queued(db, downloads_dir, monkeypatch):
    _add_books(db, "Fresh", "Already Have", "Busy")
    have = downloads_dir / "Audible" / "Already Have.m4b"
    have.parent.mkdir(parents=True)
    have.write_bytes(b"audio")
    db.add(AudibleBookDownload(asin="B003", status=STATUS_QUEUED))
    db.commit()
    monkeypatch.setattr(book_downloader, "_run", _fake_run())
    # Hold the queued "Busy" row where it is for this check.
    monkeypatch.setattr(book_downloader, "try_dispatch_queued_downloads", lambda: None)

    outcome = book_downloader.queue_many(db, ["B001", "B002", "B003", "NOPE", "B001"])

    assert outcome == {
        "queued": ["B001"],
        "already_downloaded": ["B002"],
        "in_progress": ["B003"],
        "unknown": ["NOPE"],
    }


@pytest.mark.asyncio
async def test_a_book_recorded_from_a_scan_is_not_downloaded_again(db, downloads_dir, monkeypatch):
    _add_books(db, "Somewhere Else")
    elsewhere = downloads_dir / "my-library" / "whatever name.m4b"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"audio")
    db.add(AudibleLocalFile(asin="B001", path=str(elsewhere), size_bytes=5, source=SOURCE_SCAN))
    db.commit()

    outcome = book_downloader.queue_many(db, ["B001"])

    assert outcome["already_downloaded"] == ["B001"]
    assert db.query(AudibleBookDownload).count() == 0


@pytest.mark.asyncio
async def test_a_recorded_file_that_has_since_been_deleted_downloads_again(db, downloads_dir, monkeypatch):
    _add_books(db, "Gone")
    db.add(AudibleLocalFile(asin="B001", path=str(downloads_dir / "gone.m4b"), size_bytes=5, source=SOURCE_SCAN))
    db.commit()
    monkeypatch.setattr(book_downloader, "_run", _fake_run())

    assert book_downloader.queue_many(db, ["B001"])["queued"] == ["B001"]


@pytest.mark.asyncio
async def test_force_redownloads_a_book_that_is_already_on_disk(db, downloads_dir, monkeypatch):
    _add_books(db, "Have It")
    have = downloads_dir / "Audible" / "Have It.m4b"
    have.parent.mkdir(parents=True)
    have.write_bytes(b"audio")
    monkeypatch.setattr(book_downloader, "_run", _fake_run())

    assert book_downloader.queue_many(db, ["B001"], force=True)["queued"] == ["B001"]


def test_a_book_missing_from_the_library_by_dispatch_time_fails_cleanly(db):
    db.add(AudibleBookDownload(asin="B404", status=STATUS_QUEUED))
    db.commit()

    book_downloader.try_dispatch_queued_downloads()

    db.expire_all()
    job = db.query(AudibleBookDownload).one()
    assert job.status == STATUS_FAILED
    assert "no longer in your Audible library" in job.error_message


def test_sweep_fails_running_rows_but_leaves_queued_ones(db):
    db.add(AudibleBook(asin="B001", title="A"))
    db.add(AudibleBook(asin="B002", title="B"))
    db.add(AudibleBookDownload(asin="B001", status=STATUS_RUNNING))
    db.add(AudibleBookDownload(asin="B002", status=STATUS_QUEUED))
    db.commit()

    book_downloader.sweep_stale_downloads()

    db.expire_all()
    by_asin = {j.asin: j for j in db.query(AudibleBookDownload).all()}
    assert by_asin["B001"].status == STATUS_FAILED
    assert by_asin["B002"].status == STATUS_QUEUED


def test_get_active_jobs_lists_running_and_queued_with_progress(db):
    _add_books(db, "Running Book", "Queued Book")
    db.add(AudibleBookDownload(asin="B001", status=STATUS_RUNNING, progress_bytes=50, expected_size_bytes=200))
    db.add(AudibleBookDownload(asin="B002", status=STATUS_QUEUED))
    db.add(AudibleBookDownload(asin="B001", status=STATUS_COMPLETED))
    db.commit()

    jobs = book_downloader.get_active_jobs(db)

    assert [(j["title"], j["status"], j["bytes_done"], j["bytes_total"]) for j in jobs] == [
        ("Running Book", STATUS_RUNNING, 50, 200),
        ("Queued Book", STATUS_QUEUED, 0, 0),
    ]


# -------------------------------------------------------- downloads page


def test_downloads_page_shows_queued_audiobooks_next_to_bundle_jobs(authed_client, db):
    _add_books(db, "Queued Book")
    db.add(AudibleBookDownload(asin="B001", status=STATUS_QUEUED))
    db.commit()

    resp = authed_client.get("/downloads/active")

    assert "Queued Book" in resp.text
    assert 'href="/audible/B001"' in resp.text
    assert "Audible" in resp.text
    assert "No download running" not in resp.text


def test_downloads_history_lists_audiobook_downloads_and_filters_them(authed_client, db):
    _add_books(db, "Finished Book", "Failed Book")
    db.add(AudibleBookDownload(asin="B001", status=STATUS_COMPLETED, downloaded_path="/books/Finished Book.m4b",
                               expected_size_bytes=2048, completed_at=datetime.utcnow()))
    db.add(AudibleBookDownload(asin="B002", status=STATUS_FAILED, error_message="boom"))
    db.commit()

    everything = authed_client.get("/downloads/history")
    assert "Finished Book" in everything.text
    assert "/books/Finished Book.m4b" in everything.text
    assert "m4b" in everything.text

    failed_only = authed_client.get("/downloads/history", params={"status": "failed"})
    assert "Failed Book" in failed_only.text
    assert "Finished Book" not in failed_only.text

    by_title = authed_client.get("/downloads/history", params={"q": "Finished"})
    assert "Finished Book" in by_title.text
    assert "Failed Book" not in by_title.text

    other_format = authed_client.get("/downloads/history", params={"file_format": "PDF"})
    assert "Finished Book" not in other_format.text


# ----------------------------------------------------------- audible page


def _connect(db):
    db.add(Credential(source=SOURCE_AUDIBLE, status=STATUS_OK, encrypted_payload=encrypt_json({})))
    db.commit()


def test_bulk_download_route_queues_selected_books_and_reports_the_outcome(authed_client, db, monkeypatch):
    _connect(db)
    _add_books(db, "One", "Two")
    monkeypatch.setattr(book_downloader, "try_dispatch_queued_downloads", lambda: None)

    resp = authed_client.post("/audible/download", data={"asins": ["B001", "B002", "NOPE"]})

    assert resp.status_code == 200
    assert "Queued 2" in resp.text
    assert "1 not found" in resp.text
    assert "/downloads" in resp.text
    assert db.query(AudibleBookDownload).filter(AudibleBookDownload.status == STATUS_QUEUED).count() == 2


def test_bulk_download_route_asks_for_a_selection_when_nothing_is_checked(authed_client, db):
    _connect(db)

    resp = authed_client.post("/audible/download", data={})

    assert "Select at least one book" in resp.text
    assert db.query(AudibleBookDownload).count() == 0


def test_library_table_has_checkboxes_and_marks_books_already_on_disk(authed_client, db):
    _connect(db)
    _add_books(db, "On Disk Book", "Other Book")
    db.add(AudibleLocalFile(asin="B001", path="/books/x.m4b", size_bytes=1, source=SOURCE_SCAN))
    db.commit()

    resp = authed_client.get("/audible")

    assert resp.text.count('name="asins"') == 2
    assert resp.text.count(">On disk<") == 1


def test_detail_page_shows_a_scanned_book_as_downloaded(authed_client, db, tmp_path):
    _connect(db)
    _add_books(db, "Scanned Book")
    f = tmp_path / "Scanned Book.m4b"
    f.write_bytes(b"audio")
    db.add(AudibleLocalFile(asin="B001", path=str(f), size_bytes=5, source=SOURCE_SCAN))
    db.commit()

    resp = authed_client.get("/audible/B001")
    assert "Downloaded" in resp.text
    assert str(f) in resp.text
    assert "Download audiobook" not in resp.text

    file_resp = authed_client.get("/audible/B001/book/file")
    assert file_resp.status_code == 200
    assert file_resp.content == b"audio"


def test_detail_page_says_when_a_download_is_queued(authed_client, db):
    _connect(db)
    _add_books(db, "Waiting Book")
    db.add(AudibleBookDownload(asin="B001", status=STATUS_QUEUED))
    db.commit()

    assert "waiting for a download slot" in authed_client.get("/audible/B001").text


# ------------------------------------------------------------------ scan


def _touch(path, size=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_scan_matches_files_to_titles_ignoring_case_and_punctuation(db, tmp_path):
    _add_books(db, "Dune: Part One", "Project Hail Mary")
    _touch(tmp_path / "Dune  Part One.m4b")       # what this app's own downloader writes
    _touch(tmp_path / "sub" / "project_hail_mary.M4B")
    _touch(tmp_path / "unrelated.m4b")
    _touch(tmp_path / "notes.txt")

    result = audible_scan.scan_folder(tmp_path, audible_scan.build_title_index(db), {})

    assert {m["asin"] for m in result.matched} == {"B001", "B002"}
    assert result.unmatched_count == 1
    assert result.ambiguous == []


def test_scan_never_auto_matches_two_books_with_the_same_normalized_title(db, tmp_path):
    db.add(AudibleBook(asin="B001", title="Dune", author="Frank Herbert"))
    db.add(AudibleBook(asin="B002", title="DUNE!", author="Someone Else"))
    db.commit()
    _touch(tmp_path / "Dune.m4b")

    result = audible_scan.scan_folder(tmp_path, audible_scan.build_title_index(db), {})

    assert result.matched == []
    assert len(result.ambiguous) == 1
    assert {c["asin"] for c in result.ambiguous[0]["candidates"]} == {"B001", "B002"}


def test_scan_preview_writes_nothing_and_commit_remembers_the_files(authed_client, db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    _add_books(db, "Remembered Book")
    found = _touch(tmp_path / "Remembered Book.m4b", size=7)

    preview = authed_client.post("/downloads/scan/audible", data={"folder": str(tmp_path)})
    assert preview.status_code == 200
    assert "Remembered Book" in preview.text
    assert db.query(AudibleLocalFile).count() == 0

    commit = authed_client.post("/downloads/scan/audible/commit", data={"folder": str(tmp_path)})
    assert "Recorded 1 audiobook" in commit.text
    row = db.get(AudibleLocalFile, "B001")
    assert (row.path, row.size_bytes, row.source) == (str(found), 7, SOURCE_SCAN)


def test_a_recorded_scan_survives_a_library_refresh(db, tmp_path):
    _add_books(db, "Survivor")
    found = _touch(tmp_path / "Survivor.m4b")
    result = audible_scan.scan_folder(tmp_path, audible_scan.build_title_index(db), {})
    audible_scan.commit_matches(db, result)

    # What audible_sync does on every refresh.
    db.query(AudibleBook).delete()
    db.commit()

    assert db.get(AudibleLocalFile, "B001").path == str(found)


def test_committing_the_same_scan_twice_is_a_no_op_and_rescan_counts_it_as_tracked(db, tmp_path):
    _add_books(db, "Twice")
    _touch(tmp_path / "Twice.m4b")
    index = audible_scan.build_title_index(db)

    first = audible_scan.scan_folder(tmp_path, index, {})
    assert audible_scan.commit_matches(db, first) == 1
    assert audible_scan.commit_matches(db, first) == 0

    again = audible_scan.scan_folder(tmp_path, index, audible_scan.tracked_paths(db))
    assert again.matched == []
    assert again.already_tracked_count == 1


def test_commit_selected_paths_only_records_what_was_checked(authed_client, db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    _add_books(db, "First", "Second")
    first = _touch(tmp_path / "First.m4b")
    _touch(tmp_path / "Second.m4b")

    authed_client.post("/downloads/scan/audible/commit", data={"folder": str(tmp_path), "selected_paths": [str(first)]})

    assert [r.asin for r in db.query(AudibleLocalFile).all()] == ["B001"]


def test_scan_rejects_a_folder_outside_the_configured_roots(authed_client, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr("app.config.settings.scan_root_dir", root)

    resp = authed_client.post("/downloads/scan/audible", data={"folder": "../.."})

    assert "is not a directory this app can see" in resp.text


def test_scan_allows_a_subpath_typed_with_a_leading_slash(authed_client, db, tmp_path, monkeypatch):
    """Regression test for a real bug report: the account's scan root was
    mounted at E:/, and the folder actually wanted was E:/Audio Books by
    Auth/ — typing "/Audio Books by Auth" (the natural way to express "this
    root's own subfolder") hit Path.__truediv__'s own quirk where a
    right-hand operand that looks absolute silently discards the left side:
    root / "/x" is just "/x", not "root/x" — so the configured root was
    dropped entirely and the resulting path never existed.
    """
    _add_books(db, "Dune")
    monkeypatch.setattr("app.config.settings.scan_root_dir", tmp_path)
    subdir = tmp_path / "Audio Books by Auth"
    subdir.mkdir()
    _touch(subdir / "Dune.m4b")

    resp = authed_client.post("/downloads/scan/audible", data={"folder": "/Audio Books by Auth"})

    assert "is not a directory this app can see" not in resp.text
    assert "Dune" in resp.text


def test_a_completed_download_is_remembered_as_a_local_file(db, tmp_path):
    _add_books(db, "Done")
    m4b = _touch(tmp_path / "Done.m4b", size=9)

    book_downloader.record_local_file(db, "B001", m4b)

    row = db.get(AudibleLocalFile, "B001")
    assert (row.path, row.size_bytes, row.source) == (str(m4b), 9, "download")
