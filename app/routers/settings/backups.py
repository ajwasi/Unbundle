import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app import backup
from app.csrf import require_csrf
from app.db import SessionLocal
from app.deps import get_db
from app.templates_env import templates

router = APIRouter()

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def backup_context(db: Session, backup_error: str | None = None) -> dict:
    cfg = backup.get_or_create_backup_settings(db)
    return {
        "backups_supported": backup.backups_supported(),
        "backup_enabled": cfg.enabled,
        "backup_daily_time_utc": cfg.daily_time_utc,
        "backup_retention_count": cfg.retention_count,
        "backup_last_backup_at": cfg.last_backup_at,
        "backup_error": backup_error,
        "backups": backup.list_backups(),
    }


@router.post("/backups/config", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def save_backup_config(
    request: Request,
    enabled: bool = Form(False),
    daily_time_utc: str = Form("03:00"),
    retention_count: int = Form(7),
    db: Session = Depends(get_db),
):
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", backup_context(db)
        )
    error = None
    if not _TIME_PATTERN.match(daily_time_utc):
        error = "Time must be in 24-hour HH:MM format, e.g. 03:00."
    elif retention_count < 1:
        error = "Keep at least 1 backup."
    else:
        cfg = backup.get_or_create_backup_settings(db)
        cfg.enabled = enabled
        cfg.daily_time_utc = daily_time_utc
        cfg.retention_count = retention_count
        db.commit()

    return templates.TemplateResponse(request, "settings/_backup_form.html", backup_context(db, error))


@router.post("/backups/run", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def run_backup_now(request: Request, db: Session = Depends(get_db)):
    # Required, not cosmetic: create_backup() -> db_file_path() now raises
    # RuntimeError on a non-SQLite backend, which the `except OSError` below
    # does not catch — without this guard a stray call here would 500
    # instead of degrading cleanly.
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", backup_context(db)
        )
    error = None
    try:
        backup.create_backup(db)
    except OSError as exc:
        error = f"Backup failed: {exc}"
    return templates.TemplateResponse(request, "settings/_backup_form.html", backup_context(db, error))


@router.get("/backups/{filename}/download")
def download_backup(filename: str):
    # Checked against the real directory listing rather than sanitized-and-joined
    # — no path-traversal surface at all since the value must already be one of
    # the files actually present, never a caller-controlled path.
    if filename not in backup.valid_backup_names():
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(backup.backups_dir() / filename, filename=filename, media_type="application/octet-stream")


@router.post("/backups/{filename}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def delete_backup_route(request: Request, filename: str, db: Session = Depends(get_db)):
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", backup_context(db)
        )
    error = None if backup.delete_backup(filename) else "Backup not found"
    return templates.TemplateResponse(request, "settings/_backup_form.html", backup_context(db, error))


@router.post("/backups/restore", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def restore_backup_route(request: Request, backup_file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", backup_context(db)
        )
    error = None
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "upload.db"
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(backup_file.file, f)

        try:
            backup.restore_backup(db, tmp_path)
        except backup.InvalidBackupFile as exc:
            error = str(exc)
        except Exception as exc:
            error = f"Restore failed: {exc}"

    if error:
        return templates.TemplateResponse(request, "settings/_backup_form.html", backup_context(db, error))

    # db's connection may still be attached to the just-replaced file's old,
    # now-orphaned inode (see restore_backup's own docstring) — read the
    # freshly-restored state back with a brand new session rather than reusing it.
    fresh_db = SessionLocal()
    try:
        context = backup_context(fresh_db)
    finally:
        fresh_db.close()
    return templates.TemplateResponse(request, "settings/_backup_form.html", context)
