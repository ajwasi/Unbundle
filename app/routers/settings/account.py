from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import accounts
from app.cli import _set_password
from app.csrf import require_csrf
from app.deps import get_db
from app.oidc import is_password_login_active
from app.security import check_app_password
from app.templates_env import templates

router = APIRouter()


def account_context(db: Session, account_error: str | None = None, saved: bool = False) -> dict:
    cfg = accounts.get_or_create_account_settings(db)
    return {
        "account_email": cfg.email or "",
        "account_password_active": is_password_login_active(db),
        "account_error": account_error,
        "account_saved": saved,
    }


@router.post("/account", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def save_account(
    request: Request,
    email: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip()
    password_active = is_password_login_active(db)
    wants_password_change = password_active and (current_password or new_password or confirm_password)

    error = None
    if wants_password_change:
        if not check_app_password(current_password, db):
            error = "Current password is incorrect."
        elif not new_password:
            error = "New password cannot be empty."
        elif new_password != confirm_password:
            error = "New passwords do not match."

    # Email always saves regardless of password-validation outcome — matches
    # oidc.save_oidc's own "always persist what was typed" convention rather
    # than an atomic all-or-nothing save.
    cfg = accounts.get_or_create_account_settings(db)
    cfg.email = email or None
    db.commit()

    if wants_password_change and not error:
        _set_password(new_password)

    return templates.TemplateResponse(request, "settings/_account_form.html", account_context(db, error, saved=not error))
