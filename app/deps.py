from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import settings
from app.db import SessionLocal, get_db  # get_db re-exported: routers do `db: Session = Depends(get_db)`
from app.oidc import is_oidc_enabled, is_password_disabled
from app.security import SESSION_COOKIE_NAME, verify_session_token

__all__ = ["get_db", "AuthMiddleware"]

_PUBLIC_PATH_PREFIXES = ("/login", "/auth/oidc", "/static")


class AuthMiddleware(BaseHTTPMiddleware):
    """Gates the whole UI behind a session cookie whenever some login method is
    actually configured — either APP_PASSWORD or an enabled OIDC provider (see
    app/oidc.py). Matches the original app-password-only behavior when OIDC was
    never a thing: no login method configured at all means the app stays wide
    open (unchanged prior default, not something this feature should tighten).

    OIDC config lives in the DB (editable from Settings at runtime), so this opens
    a short-lived session directly rather than via FastAPI's Depends — middleware
    runs outside the request's dependency-injection graph.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(_PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        db = SessionLocal()
        try:
            password_allowed = bool(settings.app_password) and not is_password_disabled(db)
            oidc_allowed = is_oidc_enabled(db)
        finally:
            db.close()

        if not (password_allowed or oidc_allowed):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not verify_session_token(token):
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)

        return await call_next(request)
