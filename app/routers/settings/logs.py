from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app import applog
from app.csrf import require_csrf
from app.templates_env import templates

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return templates.TemplateResponse(request, "settings/logs.html", {"log_lines": applog.recent_lines()})


@router.get("/logs/content", response_class=HTMLResponse)
def logs_content(request: Request):
    """Polled every few seconds by logs.html while the page is open, so a
    live login attempt (Audible or otherwise) can be watched without
    manually reloading."""
    return templates.TemplateResponse(request, "settings/_logs_content.html", {"log_lines": applog.recent_lines()})


@router.get("/logs/download")
def download_logs():
    return PlainTextResponse(
        applog.as_text(),
        headers={"Content-Disposition": f'attachment; filename="{applog.download_filename()}"'},
    )


@router.post("/logs/clear", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def clear_logs(request: Request):
    applog.clear()
    return templates.TemplateResponse(request, "settings/_logs_content.html", {"log_lines": applog.recent_lines()})
