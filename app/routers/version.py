"""Manual "check for updates" trigger for the version-box in the sidebar
(templates/_version_box.html) — app/version.py's own background loop already
checks every 6 hours, this just lets a user get an immediate answer instead
of waiting for it, the same "Refresh" pattern already used for Steam/GOG/the
storefront listing elsewhere in this app.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import version
from app.csrf import require_csrf
from app.ratelimit import RateLimiter, rate_limit
from app.templates_env import templates

router = APIRouter(prefix="/version")
_check_limiter = RateLimiter(max_calls=5, period_seconds=60)


@router.post(
    "/check",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_check_limiter, "version-check")), Depends(require_csrf)],
)
async def check_now(request: Request):
    checked_ok = await version.check_for_update()
    return templates.TemplateResponse(request, "_version_box.html", {"check_failed": not checked_ok})
