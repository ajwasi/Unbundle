"""Shared "best-effort cleanup, never let it mask the real error" helper.

Every streaming downloader (audible/book_downloader.py,
audible/pdf_downloader.py, amazon_music/downloader.py) independently wrote
the same `path.unlink(missing_ok=True)` call right after recording a
failure on the job row — `missing_ok=True` only swallows
FileNotFoundError, so any OTHER OSError there (confirmed live: a
destination mounted read-only, see path.unlink's own call sites) propagates
straight out of an async background task with nothing left to catch it,
surfacing only as asyncio's own "Task exception was never retrieved" log
line. The original, actually-useful error was already committed to the
job row by that point — a cleanup failure on top of it shouldn't cost
anything beyond a log line, let alone silently swallow the one at
book_downloader.py's own happy-path cleanup (there, the unlink happens
*before* the row gets marked completed, so an unhandled failure there
left a fully-downloaded file with its job stuck at "running" forever,
not just noisy logs).
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def safe_unlink(path: Path) -> None:
    """Deletes path if present; logs and continues on any other failure
    (permissions, a read-only mount, etc.) rather than raising."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove %s during cleanup: %s", path, exc)
