from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.db import SessionLocal, get_db  # get_db re-exported: routers do `db: Session = Depends(get_db)`
from app.oidc import is_auth_configured
from app.security import SESSION_COOKIE_NAME, verify_session_token

__all__ = ["get_db", "AuthMiddleware"]

_PUBLIC_PATH_PREFIXES = ("/login", "/auth/oidc")
_STATIC_PREFIX = "/static"


class AuthMiddleware(BaseHTTPMiddleware):
    """Gates the whole UI behind a session cookie whenever some login method is
    actually configured — either APP_PASSWORD or an enabled OIDC provider (see
    app/oidc.py). Matches the original app-password-only behavior when OIDC was
    never a thing: no login method configured at all means the app stays wide
    open (unchanged prior default, not something this feature should tighten).

    That "wide open" state is silent by design at the HTTP layer (nothing to
    gate), but it's exactly the state a self-hosted deployment should never sit
    in unnoticed — request.state.auth_configured is stashed here so base.html
    can render a site-wide warning banner, and main.py's startup check uses the
    same is_auth_configured() so the two can't drift apart.

    OIDC config lives in the DB (editable from Settings at runtime), so this opens
    a short-lived session directly rather than via FastAPI's Depends — middleware
    runs outside the request's dependency-injection graph.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(_STATIC_PREFIX):
            return await call_next(request)

        db = SessionLocal()
        try:
            auth_configured = is_auth_configured(db)
        finally:
            db.close()
        request.state.auth_configured = auth_configured

        if request.url.path.startswith(_PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        if not auth_configured:
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not verify_session_token(token):
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)

        return await call_next(request)
