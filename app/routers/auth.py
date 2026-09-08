"""Session-cookie login — either a plain app password (audiobook-tracker's original
convention) or OIDC/SSO (see app/oidc.py), whichever are currently configured/enabled.
Both paths converge on the same session cookie, so nothing downstream of login needs
to know or care which method was actually used.
"""

from urllib.parse import urlparse

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.oidc import build_oauth_client, get_oidc_config, is_oidc_enabled, is_password_disabled
from app.ratelimit import RateLimiter, rate_limit
from app.security import SESSION_COOKIE_NAME, check_app_password, create_session_token
from app.templates_env import templates

router = APIRouter()

# Timing-safe comparison (security.py) protects against reading the password back
# via response-time differences, but says nothing about attempt *rate* — this bounds
# it separately. 10/min is loose enough that a real user fumbling their password
# won't hit it, but bounds automated guessing to ~14k attempts/day per source IP.
_login_limiter = RateLimiter(max_calls=10, period_seconds=60)


def _is_safe_next(next: str | None) -> bool:
    # `next` round-trips through a query param, a hidden form field, and a session
    # value — all attacker-suppliable, none of it meant to ever leave this app.
    # urlparse() catches "//evil.com" (netloc set, no scheme) and "https://evil.com"
    # (scheme+netloc set) directly — confirmed via a real interpreter check, since
    # it's not obvious a bare "//..." parses with an empty scheme but a real netloc.
    # It does NOT catch a leading "/\" (some browsers historically treated
    # "/\evil.com" as protocol-relative too) — urlparse treats "\" as an ordinary
    # path character, not a delimiter — so that's still checked explicitly.
    if not next or not next.startswith("/") or next.startswith("/\\"):
        return False
    parsed = urlparse(next)
    return not parsed.scheme and not parsed.netloc


def _login_context(db: Session, next: str, error: str | None) -> dict:
    if _is_safe_next(next):
        safe_next = next
    else:
        safe_next = "/"
    return {
        "next": safe_next,
        "error": error,
        "oidc_enabled": is_oidc_enabled(db),
        "password_enabled": not is_password_disabled(db),
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "auth/login.html", _login_context(db, next, None))


@router.post("/login", dependencies=[Depends(rate_limit(_login_limiter, "login")), Depends(require_csrf)])
def login_submit(request: Request, password: str = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    if is_password_disabled(db) or not check_app_password(password, db):
        context = _login_context(db, next, "Incorrect password")
        return templates.TemplateResponse(request, "auth/login.html", context, status_code=401)
    # The RedirectResponse call itself lives inside each branch (rather than being
    # fed by a ternary-computed variable afterward) — CodeQL's open-redirect query
    # kept flagging this exact line through two prior attempts that were logically
    # equivalent (_safe_next() returning a cleaned value; then an inline
    # `x if _is_safe_next(x) else "/"` ternary) but weren't real if/else statement
    # branches. Matches the shape that was separately confirmed to work for this
    # app's SSRF alert (storefront.py: fetch_bundle_detail).
    if _is_safe_next(next):
        response = RedirectResponse(url=next, status_code=303)
    else:
        response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, create_session_token(), httponly=True, samesite="lax")
    return response


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/auth/oidc/login")
async def oidc_login(request: Request, next: str = "/", db: Session = Depends(get_db)):
    cfg = get_oidc_config(db)
    if not cfg or not cfg.get("enabled"):
        raise HTTPException(status_code=404, detail="OIDC is not configured")
    if _is_safe_next(next):
        request.session["oidc_next"] = next
    else:
        request.session["oidc_next"] = "/"
    client = build_oauth_client(cfg)
    redirect_uri = str(request.url_for("oidc_callback"))
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, db: Session = Depends(get_db)):
    cfg = get_oidc_config(db)
    if not cfg or not cfg.get("enabled"):
        raise HTTPException(status_code=404, detail="OIDC is not configured")

    client = build_oauth_client(cfg)
    try:
        await client.authorize_access_token(request)
    except OAuthError as exc:
        return templates.TemplateResponse(
            request, "auth/login.html", _login_context(db, "/", f"SSO login failed: {exc.description or exc.error}"),
            status_code=401,
        )

    stored_next = request.session.pop("oidc_next", "/")
    if _is_safe_next(stored_next):
        response = RedirectResponse(url=stored_next, status_code=303)
    else:
        response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, create_session_token(), httponly=True, samesite="lax")
    return response
