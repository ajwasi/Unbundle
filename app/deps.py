from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app import accounts, api_tokens
from app.config import settings
from app.csrf import COOKIE_NAME as CSRF_COOKIE_NAME, ensure_csrf_cookie
from app.db import SessionLocal, get_db  # get_db re-exported: routers do `db: Session = Depends(get_db)`
from app.oidc import is_auth_configured
from app.security import SESSION_COOKIE_NAME, decode_session_token

__all__ = ["get_db", "AuthMiddleware", "SecurityHeadersMiddleware"]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers CSRF protection doesn't cover. The double-submit CSRF cookie
    (csrf.py) only stops a *forged* cross-origin request — it does nothing
    against clickjacking, where an attacker iframes this app's real page (with
    the victim's real cookies, real CSRF token, everything genuine) and tricks
    a click into landing on a real button. X-Frame-Options/frame-ancestors is
    the actual defense for that, and there's no reason for this single-user
    app to ever be framed by anything, including itself. The other two are
    cheap, standard defense-in-depth with no functional downside: nosniff
    stops a MIME-sniffing attack on an upload-adjacent response, and a same-
    origin referrer policy keeps this app's own URLs (which can carry a
    gamekey or similar in the path) out of a third-party site's referrer logs
    when an outbound link (e.g. a redeem-on-humblebundle.com link) is clicked.

    Deliberately no Content-Security-Policy here — this app relies on several
    inline `<script>` blocks (countdown timers, the image-fallback handler,
    docs/_content.html's theme detection) and one external CDN script
    (ReDoc), and a CSP strict enough to matter would need either
    'unsafe-inline' (which defeats most of the point) or per-response nonces
    threaded through every template that has an inline script — a much larger
    change than the header additions here, not attempted in this pass.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

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

    An `Authorization: Bearer <token>` header (see app/api_tokens.py, created
    from Settings) is an alternate, equally-privileged path in for a
    non-browser client — checked first, and its own failure mode (401 JSON,
    not a redirect) rather than falling back to the cookie check, since a
    client that already sent a bearer token is unambiguously not a browser.

    request.state.auth_configured is still stashed here so base.html's warning
    banner and main.py's startup check keep working as a defense-in-depth
    fallback (harmless, and covers the pre-/setup "unknown" instant a process
    restarts) even though normal flow should never actually reach a page in
    that state anymore.

    request.state.identity_label is resolved here too (once a session is
    confirmed valid) — the only place this happens — so base.html's top-right
    user menu can read it directly, the same way it already reads
    auth_configured/csrf_token. None on any page a logged-out request can
    reach (login/setup/public paths).

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

        # One shared session for both DB reads below (auth check, then identity
        # label) rather than one SessionLocal() per read — both happen back to
        # back before call_next either way, so this doesn't hold the connection
        # open any longer than the original two-session version did, it just
        # halves the pool checkouts (and, on Postgres, pool_pre_ping's per-checkout
        # liveness ping) for every authenticated request.
        db = SessionLocal()
        try:
            auth_configured = is_auth_configured(db)
            request.state.auth_configured = auth_configured
            request.state.identity_label = None  # overwritten below once a session is confirmed valid
            request.state.api_token_authed = False  # overwritten below on a valid bearer token

            if not auth_configured and not request.url.path.startswith("/setup"):
                return _finish(RedirectResponse(url="/setup", status_code=303))

            if not request.url.path.startswith(_PUBLIC_PATH_PREFIXES):
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    # An explicit bearer token unambiguously marks this as a
                    # non-browser client — on failure it gets a 401, never the
                    # redirect-to-/login a browser would get, which would be
                    # meaningless to a script anyway.
                    token_row = api_tokens.verify_token(db, auth_header[len("Bearer "):])
                    if token_row is None:
                        return _finish(JSONResponse({"detail": "Invalid or missing API token"}, status_code=401))
                    request.state.identity_label = f"API token: {token_row.name}"
                    request.state.api_token_authed = True
                else:
                    token = request.cookies.get(SESSION_COOKIE_NAME)
                    session_payload = decode_session_token(token)
                    if session_payload is None:
                        return _finish(RedirectResponse(url=f"/login?next={request.url.path}", status_code=303))
                    request.state.identity_label = accounts.resolve_identity_label(session_payload.get("identity"), db)
        finally:
            db.close()

        return _finish(await call_next(request))
