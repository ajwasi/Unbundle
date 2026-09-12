"""Cross-bundle downloads: the currently-running job, full history of
individual file downloads, destination routing rules, and a folder-scan tool
for marking files that predate this feature (or arrived outside the app) as
already downloaded. This router is the UI/query layer only — download
execution lives in downloads/worker.py, relocation in downloads/relocate.py,
scan matching in downloads/scan.py.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.csrf import require_csrf
from app.deps import get_db
from app.downloads import worker
from app.downloads.scan import build_expected_index, commit_matches, scan_folder
from app.models.bundle import Bundle
from app.models.download import Download
from app.models.download_destination import DownloadDestination
from app.models.download_settings import DownloadSettings
from app.models.tag import Tag
from app.templates_env import templates

router = APIRouter(prefix="/downloads")

# A scan of a large, long-lived library can turn up thousands of matched
# and/or ambiguous rows (confirmed live: 7000+ matched, 4000+ ambiguous
# against one real folder) — rendering all of them in one response makes for
# a multi-megabyte page that's genuinely slow to load and scroll, separate
# from whether the commit mechanism itself works. Commit still always acts
# on the full re-scanned result (see scan_commit below), never just this
# displayed slice — only what's *shown* in the preview is capped.
_SCAN_PREVIEW_LIMIT = 200


def _destination_rows(db: Session) -> list[dict]:
    tags_by_id = {t.id: t.name for t in db.query(Tag).all()}
    return [
        {
            "id": d.id,
            "name": d.name,
            "formats": d.formats,
            "tag_name": tags_by_id.get(d.tag_id) if d.tag_id else None,
            "path": d.path,
        }
        for d in db.query(DownloadDestination).order_by(DownloadDestination.id).all()
    ]


def _destinations_context(db: Session) -> dict:
    return {"destinations": _destination_rows(db), "all_tags": db.query(Tag).order_by(Tag.name).all()}


def _concurrency_context(db: Session) -> dict:
    row = db.get(DownloadSettings, 1)
    return {"concurrency": row.concurrency if row is not None else settings.download_concurrency}


def _history_context(db: Session, status: str, file_format: str, q: str) -> dict:
    query = db.query(Download, Bundle.name).join(Bundle, Bundle.gamekey == Download.gamekey)
    if status:
        query = query.filter(Download.status == status)
    if file_format:
        query = query.filter(Download.file_format == file_format)
    if q:
        query = query.filter(Bundle.name.ilike(f"%{q}%"))
    rows = [
        {"download": d, "bundle_name": bundle_name}
        for d, bundle_name in query.order_by(Download.id.desc()).all()
    ]
    all_formats = sorted({fmt for (fmt,) in db.query(Download.file_format).distinct().all() if fmt})
    return {"rows": rows, "status": status, "file_format": file_format, "q": q, "all_formats": all_formats}


@router.get("", response_class=HTMLResponse)
def downloads_page(
    request: Request,
    status: str = "",
    file_format: str = "",
    q: str = "",
    db: Session = Depends(get_db),
):
    context: dict = {"active_jobs": worker.get_active_jobs(db), "scan_roots": _scan_root_choices()}
    context.update(_history_context(db, status, file_format, q))
    context.update(_destinations_context(db))
    context.update(_concurrency_context(db))
    return templates.TemplateResponse(request, "downloads/list.html", context)


@router.get("/active", response_class=HTMLResponse)
def active_status(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "downloads/_active.html", {"active_jobs": worker.get_active_jobs(db)})


@router.get("/history", response_class=HTMLResponse)
def history(
    request: Request,
    status: str = "",
    file_format: str = "",
    q: str = "",
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request, "downloads/_history.html", _history_context(db, status, file_format, q)
    )


@router.post("/destinations", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def create_destination(
    request: Request,
    name: str = Form(default=""),
    formats: str = Form(default=""),
    tag_id: str = Form(default=""),
    path: str = Form(default=""),
    db: Session = Depends(get_db),
):
    resolved_tag_id = int(tag_id) if tag_id.strip().isdigit() else None
    if name.strip() and path.strip():
        db.add(
            DownloadDestination(
                name=name.strip(), formats=formats.strip(), tag_id=resolved_tag_id, path=path.strip()
            )
        )
        db.commit()
    return templates.TemplateResponse(request, "downloads/_destinations.html", _destinations_context(db))


@router.post("/destinations/{destination_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def delete_destination(request: Request, destination_id: int, db: Session = Depends(get_db)):
    dest = db.get(DownloadDestination, destination_id)
    if dest is not None:
        db.delete(dest)
        db.commit()
    return templates.TemplateResponse(request, "downloads/_destinations.html", _destinations_context(db))


@router.post("/concurrency", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def save_concurrency(request: Request, concurrency: str = Form(default=""), db: Session = Depends(get_db)):
    if concurrency.strip().isdigit() and int(concurrency) >= 1:
        row = db.get(DownloadSettings, 1)
        if row is None:
            row = DownloadSettings(id=1)
            db.add(row)
        row.concurrency = int(concurrency)
        db.commit()
    return templates.TemplateResponse(request, "downloads/_concurrency_form.html", _concurrency_context(db))


def _scan_root_choices() -> list[dict]:
    """[{"index": i, "label": ...}] for every configured scan root, in the
    order scan_root_list() returns them — the form's root_index submits the
    index, never the path itself, so there's no request-controlled string
    anywhere near path resolution (see _resolve_scan_folder). label is just
    the mount point's own last path segment (e.g. "/scan-root-2" -> "scan-
    root-2"); name it meaningfully in docker-compose.yml's volumes: if you
    want a nicer label here.
    """
    return [{"index": i, "label": root.name or str(root)} for i, root in enumerate(settings.scan_root_list())]


def _resolve_scan_folder(root_index: int, folder: str) -> Path | None:
    """folder-scan can only ever see one of settings.scan_root_list()'s
    configured roots and its subdirectories — never an arbitrary path from
    the request, and never a root the deployer didn't configure either:
    root_index is bounds-checked against that fixed, server-side list, not
    a path the request supplies. In Docker each configured root is a
    dedicated, admin-chosen, read-only mount (docker-compose.yml's
    SCAN_ROOT/SCAN_ROOTS), so this is a real containment boundary: whatever
    isn't mounted at one of these exact paths is genuinely unreachable to
    this feature, regardless of what a request submits — not just a
    convention this code happens to follow. An empty folder means "the
    root itself" — scanning a whole configured root is a deliberate,
    common choice once there's more than one to pick from.

    Inlined here (rather than reusing paths.resolve_within(), which does
    the same is_relative_to() check) so the containment check sits in the
    same function as its own use — a call to a separate helper function
    left CodeQL's path-injection query still flagging the result as
    tainted, even though the helper is unconditionally safe.
    """
    roots = settings.scan_root_list()
    if not (0 <= root_index < len(roots)):
        return None
    folder = folder.strip()
    root = roots[root_index].resolve()
    try:
        candidate = (root / folder).resolve() if folder else root
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_dir() else None


@router.post("/scan", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def scan_preview(request: Request, root_index: int = Form(default=0), folder: str = Form(default=""), db: Session = Depends(get_db)):
    folder = folder.strip()
    resolved = _resolve_scan_folder(root_index, folder)
    context = {"root_index": root_index, "folder": folder, "preview_limit": _SCAN_PREVIEW_LIMIT}
    if resolved is None:
        context["error"] = f'"{folder or "/"}" is not a directory this app can see.'
        return templates.TemplateResponse(request, "downloads/_scan_result.html", context)
    context["result"] = scan_folder(resolved, build_expected_index(db))
    return templates.TemplateResponse(request, "downloads/_scan_result.html", context)


@router.post("/scan/commit", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def scan_commit(request: Request, root_index: int = Form(default=0), folder: str = Form(default=""), db: Session = Depends(get_db)):
    folder = folder.strip()
    resolved = _resolve_scan_folder(root_index, folder)
    context = {"root_index": root_index, "folder": folder, "preview_limit": _SCAN_PREVIEW_LIMIT}
    if resolved is None:
        context["error"] = f'"{folder or "/"}" is not a directory this app can see.'
        return templates.TemplateResponse(request, "downloads/_scan_result.html", context)
    # Re-scan rather than trusting a client-submitted match list — cheap, and
    # never commits anything the server hasn't independently verified itself.
    result = scan_folder(resolved, build_expected_index(db))
    context["result"] = result
    context["committed"] = commit_matches(db, result)
    return templates.TemplateResponse(request, "downloads/_scan_result.html", context)
