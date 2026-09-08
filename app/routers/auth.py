"""Session-cookie login — either a plain app password (audiobook-tracker's original
convention) or OIDC/SSO (see app/oidc.py), whichever are currently configured/enabled.
Both paths converge on the same session cookie, so nothing downstream of login needs
to know or care which method was actually used.
"""

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.cli import _set_password
from app.config import settings
from app.csrf import require_csrf
from app.deps import get_db
from app.oidc import build_oauth_client, get_oidc_config, is_auth_configured, is_oidc_enabled, is_password_disabled
from app.ratelimit import RateLimiter, rate_limit
from app.security import SESSION_COOKIE_NAME, check_app_password, create_session_token
from app.templates_env import templates

router = APIRouter()

# Timing-safe comparison (security.py) protects against reading the password back
# via response-time differences, but says nothing about attempt *rate* — this bounds
# it separately. 10/min is loose enough that a real user fumbling their password
# won't hit it, but bounds automated guessing to ~14k attempts/day per source IP.
_login_limiter = RateLimiter(max_calls=10, period_seconds=60)


# `next` round-trips through a query param, a hidden form field, and a session
# value — all attacker-suppliable, none of it meant to ever leave this app. A
# leading "//" or "/\" is browser shorthand for a scheme-relative absolute URL
# (e.g. "//evil.com"), so only a genuine single-slash-rooted path counts as safe.
#
# Deliberately inlined at every use below (4 sites, listed together here so
# they're easy to grep and keep in sync — update all 4 if this rule ever
# changes) rather than a shared _is_safe_next()-style predicate function. Three
# prior attempts at exactly that (a function returning a "cleaned" value; an
# inline ternary calling a boolean predicate; a real if/else statement whose
# *condition* was still a call to that predicate) all still left CodeQL's
# open-redirect query flagging the RedirectResponse sinks below. The common
# thread: every attempt hid the actual comparison behind a named function call
# in the guarding condition, even once the sink itself was correctly inside an
# if/else statement's true branch (which *did* independently fix this app's
# separate SSRF alert in storefront.py, whose guard was already a direct
# inline comparison, never a function call). CodeQL's guard recognition for
# this query apparently needs the literal comparison visible at the point of
# the check, not merely a same-function boolean result — sink-in-branch and
# statement-vs-expression turned out not to be the deciding factors after all.
def _login_context(db: Session, next: str, error: str | None) -> dict:
    if next and next.startswith("/") and not next.startswith("//") and not next.startswith("/\\"):
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
    if next and next.startswith("/") and not next.startswith("//") and not next.startswith("/\\"):
        response = RedirectResponse(url=next, status_code=303)
    else:
        response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, create_session_token(), httponly=True, samesite="lax", secure=settings.behind_https_proxy
    )
    return response


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# AuthMiddleware (app/deps.py) forces every request here whenever no login method
# is configured at all — a deliberate reversal of this app's original "wide open,
# just warn loudly" default, per explicit user decision: a fresh deployment (or an
# existing one that's never had a password set) is unusable until this succeeds.
# Both routes re-check is_auth_configured() themselves rather than trusting the
# middleware alone, since this must never become a way to reset an existing
# password without already being logged in.
@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    if is_auth_configured(db):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "auth/setup.html", {"error": None})


@router.post("/setup", dependencies=[Depends(require_csrf)])
def setup_submit(
    request: Request, password: str = Form(""), confirm_password: str = Form(""), db: Session = Depends(get_db)
):
    if is_auth_configured(db):
        return RedirectResponse(url="/", status_code=303)

    error = None
    if not password:
        error = "Password cannot be empty."
    elif password != confirm_password:
        error = "Passwords do not match."

    if error:
        return templates.TemplateResponse(request, "auth/setup.html", {"error": error}, status_code=400)

    _set_password(password)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, create_session_token(), httponly=True, samesite="lax", secure=settings.behind_https_proxy
    )
    return response


@router.get("/auth/oidc/login")
async def oidc_login(request: Request, next: str = "/", db: Session = Depends(get_db)):
    cfg = get_oidc_config(db)
    if not cfg or not cfg.get("enabled"):
        raise HTTPException(status_code=404, detail="OIDC is not configured")
    if next and next.startswith("/") and not next.startswith("//") and not next.startswith("/\\"):
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
    if stored_next and stored_next.startswith("/") and not stored_next.startswith("//") and not stored_next.startswith("/\\"):
        response = RedirectResponse(url=stored_next, status_code=303)
    else:
        response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, create_session_token(), httponly=True, samesite="lax", secure=settings.behind_https_proxy
    )
    return response
