"""app/jobqueue.py's two shared helpers, exercised directly (the subsystems
that use them are covered by their own suites)."""

import asyncio

import pytest

from app import jobqueue
from app.models.audible_book_download import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AudibleBookDownload,
)


def _add(db, asin, status):
    job = AudibleBookDownload(asin=asin, status=status)
    db.add(job)
    db.commit()
    return job


def _status(db, asin):
    db.expire_all()
    return db.query(AudibleBookDownload).filter_by(asin=asin).one().status


@pytest.mark.asyncio
async def test_dispatch_starts_oldest_first_up_to_the_limit(db):
    for asin in ("A", "B", "C"):
        _add(db, asin, STATUS_QUEUED)
    running: set[int] = set()
    started: list[str] = []

    async def _noop():
        return None

    def make_task(_db, job):
        started.append(job.asin)
        return _noop()

    jobqueue.dispatch_queued(
        db, AudibleBookDownload, queued=STATUS_QUEUED, running=STATUS_RUNNING,
        running_ids=running, limit=2, make_task=make_task,
    )
    await asyncio.sleep(0)

    assert started == ["A", "B"]
    assert len(running) == 2
    assert [_status(db, a) for a in "ABC"] == [STATUS_RUNNING, STATUS_RUNNING, STATUS_QUEUED]


@pytest.mark.asyncio
async def test_dispatch_does_not_exceed_slots_already_taken(db):
    _add(db, "A", STATUS_QUEUED)
    jobqueue.dispatch_queued(
        db, AudibleBookDownload, queued=STATUS_QUEUED, running=STATUS_RUNNING,
        running_ids={101, 102}, limit=2, make_task=lambda _db, _job: pytest.fail("no slot free"),
    )
    assert _status(db, "A") == STATUS_QUEUED


@pytest.mark.asyncio
async def test_a_job_the_task_factory_fails_does_not_consume_a_slot(db):
    _add(db, "BAD", STATUS_QUEUED)
    _add(db, "GOOD", STATUS_QUEUED)
    running: set[int] = set()

    async def _noop():
        return None

    def make_task(_db, job):
        if job.asin == "BAD":
            job.status = STATUS_FAILED
            return None
        return _noop()

    jobqueue.dispatch_queued(
        db, AudibleBookDownload, queued=STATUS_QUEUED, running=STATUS_RUNNING,
        running_ids=running, limit=1, make_task=make_task,
    )
    await asyncio.sleep(0)

    assert _status(db, "BAD") == STATUS_FAILED
    assert _status(db, "GOOD") == STATUS_RUNNING  # took the one slot BAD didn't use
    assert len(running) == 1


def test_sweep_stale_fails_running_rows_only(db):
    _add(db, "RUN", STATUS_RUNNING)
    _add(db, "WAIT", STATUS_QUEUED)

    jobqueue.sweep_stale(AudibleBookDownload, running=STATUS_RUNNING, failed=STATUS_FAILED)

    db.expire_all()
    swept = db.query(AudibleBookDownload).filter_by(asin="RUN").one()
    assert swept.status == STATUS_FAILED
    assert swept.error_message == jobqueue.INTERRUPTED_MESSAGE
    assert swept.completed_at is not None
    assert _status(db, "WAIT") == STATUS_QUEUED
