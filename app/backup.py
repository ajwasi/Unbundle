"""Scheduled/manual SQLite backups, retention, and restore.

The one deliberately-scheduled background task in this app — everything else
(Humble refresh, downloads) is on-demand only, triggered by a request (see
sync/refresh.py's own docstring on that philosophy). Backups are the
exception: the whole point is protection during the exact stretches nobody is
looking at the app, so waiting for a request to trigger one would defeat the
purpose.

All timestamps here are naive UTC, matching this codebase's existing
convention for Humble's own (also-unmarked) timestamps elsewhere (see e.g.
models/tag.py's created_at).
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, engine
from app.models.backup_settings import BackupSettings

_SCHEDULER_POLL_SECONDS = 600  # 10 min — plenty of resolution for a once-daily schedule
_SQLITE_MAGIC = b"SQLite format 3\x00"
_EXPECTED_TABLES = {"bundle", "credential"}


class InvalidBackupFile(Exception):
    """Raised by restore_backup when the uploaded file isn't a real backup of
    this app's database — never touches the live DB when this is raised."""


def backups_dir() -> Path:
    return settings.data_dir / "backups"


def db_file_path() -> Path:
    return Path(engine.url.database)


def get_or_create_backup_settings(db: Session) -> BackupSettings:
    cfg = db.get(BackupSettings, 1)
    if cfg is None:
        cfg = BackupSettings(id=1)
        db.add(cfg)
        db.commit()
    return cfg


def _backup_filename(now: datetime) -> str:
    return f"humble-{now.strftime('%Y%m%d-%H%M%S')}.db"


def create_backup(db: Session) -> Path:
    backups_dir().mkdir(parents=True, exist_ok=True)
    dest = backups_dir() / _backup_filename(datetime.utcnow())

    src = sqlite3.connect(db_file_path())
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    cfg = get_or_create_backup_settings(db)
    cfg.last_backup_at = datetime.utcnow()
    db.commit()

    _enforce_retention(cfg.retention_count)
    return dest


def _enforce_retention(retention_count: int) -> None:
    # Filenames are timestamp-sortable, so a plain name sort is a chronological sort.
    files = sorted(backups_dir().glob("humble-*.db"))
    excess = len(files) - max(retention_count, 0)
    for old_file in files[:excess]:
        old_file.unlink(missing_ok=True)


def list_backups() -> list[dict]:
    files = sorted(backups_dir().glob("humble-*.db"), reverse=True)
    return [
        {
            "name": f.name,
            "size_bytes": f.stat().st_size,
            "created_at": datetime.utcfromtimestamp(f.stat().st_mtime),
        }
        for f in files
    ]


def _is_due(cfg: BackupSettings, now: datetime) -> bool:
    if not cfg.enabled:
        return False
    hour, minute = (int(part) for part in cfg.daily_time_utc.split(":"))
    todays_target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    most_recent_target = todays_target if now >= todays_target else todays_target - timedelta(days=1)
    return cfg.last_backup_at is None or cfg.last_backup_at < most_recent_target


async def run_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(_SCHEDULER_POLL_SECONDS)
        db = SessionLocal()
        try:
            cfg = get_or_create_backup_settings(db)
            if _is_due(cfg, datetime.utcnow()):
                create_backup(db)
        except Exception:
            # Never let one failed backup attempt kill the whole scheduler loop.
            traceback.print_exc(file=sys.stderr)
        finally:
            db.close()


def _validate_backup_file(path: Path) -> None:
    with open(path, "rb") as f:
        header = f.read(len(_SQLITE_MAGIC))
    if header != _SQLITE_MAGIC:
        raise InvalidBackupFile("That file isn't a SQLite database.")

    conn = sqlite3.connect(path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as exc:
        raise InvalidBackupFile(f"Could not read that file as a database: {exc}") from None
    finally:
        conn.close()

    missing = _EXPECTED_TABLES - tables
    if missing:
        raise InvalidBackupFile(
            f"Doesn't look like a Humble Tracker backup (missing table(s): {', '.join(sorted(missing))})."
        )


def _upgrade_to_head() -> None:
    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "app" / "alembic"))
    command.upgrade(cfg, "head")


def restore_backup(db: Session, upload_path: Path) -> None:
    """Validates first, auto-backs-up the live DB, then swaps the file and
    upgrades it to the current schema.

    engine.dispose() releases pooled connections before the swap — sessions
    are already request-scoped (app/db.py's get_db()), so this is enough to
    make the swap safe without a full process restart. The swap itself goes
    through a temp file + os.replace (atomic rename) rather than overwriting
    the live file's bytes in place, so a connection that's somehow still open
    against the old file keeps seeing a complete, consistent (if stale) file
    right up until it closes, instead of a torn write.
    """
    _validate_backup_file(upload_path)

    create_backup(db)  # safety net: a bad restore is itself undo-able a moment later

    # db's own connection must be released (not just idle pooled ones) before the
    # swap — on Windows, an open handle on the destination blocks os.replace()
    # outright (unlike POSIX rename, which just repoints the directory entry).
    db.close()
    engine.dispose()
    tmp_dest = db_file_path().with_suffix(".db.restoring")
    shutil.copyfile(upload_path, tmp_dest)
    os.replace(tmp_dest, db_file_path())

    _upgrade_to_head()
