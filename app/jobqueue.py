"""Shared mechanics for the app's DB-backed background-job queues.

Every long-running subsystem (Humble downloads, Audible books, Amazon Music,
the library refresh) follows the same pattern, described in docs/review/
architecture.md: a status row in the database is the durable truth, a plain
module-level set of running ids bounds concurrency, and a startup "sweep"
fails anything a restart left half-done. These two helpers are the parts that
were copy-pasted per subsystem.

State deliberately stays with each subsystem (its own `_running_job_ids` /
`_progress`): the app is single-process, tests reset that state per module,
and nothing here needs to own it. These helpers only operate on what they're
handed.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy.orm import Session

from app.asyncio_utils import spawn_background_task
from app.db import SessionLocal
from app.timeutil import utcnow

INTERRUPTED_MESSAGE = "Interrupted by application restart."


def sweep_stale(
    model: type,
    *,
    running: str,
    failed: str,
    finished_attr: str = "completed_at",
    message: str = INTERRUPTED_MESSAGE,
) -> None:
    """Call on app startup: anything still marked `running` belongs to a
    process that no longer exists (a container restart mid-job) and would
    otherwise sit at "running" forever. Queued rows are left alone — the
    subsystem's own dispatcher picks them back up.
    """
    db = SessionLocal()
    try:
        for row in db.query(model).filter(model.status == running).all():
            row.status = failed
            setattr(row, finished_attr, utcnow())
            row.error_message = message
        db.commit()
    finally:
        db.close()


def dispatch_queued(
    db: Session,
    model: type,
    *,
    queued: str,
    running: str,
    running_ids: set[int],
    limit: int,
    make_task: Callable[[Session, Any], Coroutine | None],
    started_attr: str = "started_at",
) -> None:
    """Starts queued jobs, oldest first, until `limit` are running.

    Each claimed job is marked `running` and committed *before* its task is
    spawned, so the next loop iteration's "oldest still-queued job" query
    can't hand back the same row. There is no `await` between checking
    `len(running_ids)` and adding to it, so two calls landing back-to-back
    can't both take the same slot.

    `make_task(db, job)` builds the coroutine to run for a claimed job. It may
    instead fail the job itself (set its own failed status/message) and return
    None, in which case nothing is spawned and the slot isn't consumed.
    """
    while len(running_ids) < limit:
        job = db.query(model).filter(model.status == queued).order_by(model.id).first()
        if job is None:
            break
        job.status = running
        setattr(job, started_attr, utcnow())
        coro = make_task(db, job)
        db.commit()
        if coro is None:
            continue
        running_ids.add(job.id)
        spawn_background_task(coro)
