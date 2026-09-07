"""Session-cookie login — either a plain app password (audiobook-tracker's original
convention) or OIDC/SSO (see app/oidc.py), whichever are currently configured/enabled.
Both paths converge on the same session cookie, so nothing downstream of login needs
to know or care which method was actually used.
"""

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.deps import get_db
from app.oidc import build_oauth_client, get_oidc_config, is_oidc_enabled, is_password_disabled
from app.security import SESSION_COOKIE_NAME, check_app_password, create_session_token
from app.templates_env import templates

router = APIRouter()


def _login_context(db: Session, next: str, error: str | None) -> dict:
    return {
        "next": next,
        "error": error,
        "oidc_enabled": is_oidc_enabled(db),
        "password_enabled": not is_password_disabled(db),
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "auth/login.html", _login_context(db, next, None))


@router.post("/login")
def login_submit(request: Request, password: str = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    if is_password_disabled(db) or not check_app_password(password, db):
        context = _login_context(db, next, "Incorrect password")
        return templates.TemplateResponse(request, "auth/login.html", context, status_code=401)
    response = RedirectResponse(url=next or "/", status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, create_session_token(), httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/auth/oidc/login")
async def oidc_login(request: Request, next: str = "/", db: Session = Depends(get_db)):
    cfg = get_oidc_config(db)
    if not cfg or not cfg.get("enabled"):
        raise HTTPException(status_code=404, detail="OIDC is not configured")
    request.session["oidc_next"] = next or "/"
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

    next_url = request.session.pop("oidc_next", "/")
    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, create_session_token(), httponly=True, samesite="lax")
    return response
