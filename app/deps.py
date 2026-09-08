from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import settings
from app.csrf import COOKIE_NAME as CSRF_COOKIE_NAME, ensure_csrf_cookie
from app.db import SessionLocal, get_db  # get_db re-exported: routers do `db: Session = Depends(get_db)`
from app.oidc import is_auth_configured
from app.security import SESSION_COOKIE_NAME, verify_session_token

__all__ = ["get_db", "AuthMiddleware"]

_PUBLIC_PATH_PREFIXES = ("/login", "/auth/oidc", "/setup")
_STATIC_PREFIX = "/static"
# Prometheus can't do an interactive login (and won't carry a CSRF cookie back on its
# next scrape either — it'd just get re-issued one on every single scrape forever), so
# this skips the whole middleware the same way /static does. Its own optional
# METRICS_TOKEN bearer check (see routers/metrics.py) is the only auth this route gets.
_METRICS_PATH = "/metrics"


class AuthMiddleware(BaseHTTPMiddleware):
    """Gates the whole UI behind a session cookie whenever some login method is
    actually configured — either APP_PASSWORD or an enabled OIDC provider (see
    app/oidc.py). No login method configured at all now forces a redirect to
    /setup rather than letting the app run wide open — a deliberate reversal of
    this app's original default, per explicit user decision (see
    routers/auth.py's /setup docstring for the full reasoning).

    request.state.auth_configured is still stashed here so base.html's warning
    banner and main.py's startup check keep working as a defense-in-depth
    fallback (harmless, and covers the pre-/setup "unknown" instant a process
    restarts) even though normal flow should never actually reach a page in
    that state anymore.

    OIDC config lives in the DB (editable from Settings at runtime), so this opens
    a short-lived session directly rather than via FastAPI's Depends — middleware
    runs outside the request's dependency-injection graph.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(_STATIC_PREFIX) or request.url.path == _METRICS_PATH:
            return await call_next(request)

        # Ensured here (rather than lazily wherever a form happens to render) so
        # it's set on every response uniformly, including the very first request
        # of a new session — the login page itself is one of the forms this
        # protects, so it can't wait until after auth.
        csrf_token = ensure_csrf_cookie(request)
        request.state.csrf_token = csrf_token
        csrf_cookie_is_new = request.cookies.get(CSRF_COOKIE_NAME) != csrf_token

        def _finish(response):
            if csrf_cookie_is_new:
                response.set_cookie(
                    CSRF_COOKIE_NAME, csrf_token, httponly=True, samesite="lax", secure=settings.behind_https_proxy
                )
            return response

        db = SessionLocal()
        try:
            auth_configured = is_auth_configured(db)
        finally:
            db.close()
        request.state.auth_configured = auth_configured

        if not auth_configured and not request.url.path.startswith("/setup"):
            return _finish(RedirectResponse(url="/setup", status_code=303))

        if request.url.path.startswith(_PUBLIC_PATH_PREFIXES):
            return _finish(await call_next(request))

        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not verify_session_token(token):
            return _finish(RedirectResponse(url=f"/login?next={request.url.path}", status_code=303))

        return _finish(await call_next(request))
