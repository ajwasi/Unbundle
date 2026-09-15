import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app import accounts, applog, backup
from app.cli import _set_password
from app.config import settings
from app.connectors import audible_connector, gog_connector, steam_connector
from app.connectors.humble_connector import HumbleConnector
from app.csrf import require_csrf
from app.db import SessionLocal
from app.deps import get_db
from app.models.credential import (
    SOURCE_AUDIBLE,
    SOURCE_GOG,
    SOURCE_HUMBLE,
    SOURCE_OIDC,
    SOURCE_STEAM,
    STATUS_ERROR,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    Credential,
)
from app.oidc import discover, get_oidc_config, is_password_login_active
from app.security import check_app_password, decrypt_json, encrypt_json
from app.sync import audible_sync, gog_sync
from app.templates_env import templates

router = APIRouter(prefix="/settings")

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _account_context(db: Session, account_error: str | None = None) -> dict:
    cfg = accounts.get_or_create_account_settings(db)
    return {
        "account_email": cfg.email or "",
        "account_password_active": is_password_login_active(db),
        "account_error": account_error,
    }


def _humble_credential(db: Session) -> Credential | None:
    return Credential.get(db, SOURCE_HUMBLE)


def _get_or_create_humble_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_HUMBLE)


def _get_or_create_oidc_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_OIDC)


def _get_or_create_steam_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_STEAM)


def _steam_context(db: Session, steam_error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_STEAM)
    payload = decrypt_json(cred.encrypted_payload) if cred and cred.encrypted_payload else {}
    return {
        "steam_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "steam_error": steam_error if steam_error is not None else (cred.last_error if cred else None),
        "steam_has_key": bool(payload.get("api_key")),
        "steam_id_display": payload.get("steamid64", ""),
    }


def _get_or_create_gog_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_GOG)


def _gog_context(db: Session, error: str | None = None) -> dict:
    cred = Credential.get(db, SOURCE_GOG)
    return {
        "gog_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "gog_error": error if error is not None else (cred.last_error if cred else None),
        "gog_login_url": gog_connector.LOGIN_URL,
        "demo_mode": settings.demo_mode,
    }


def _gog_response(
    request: Request,
    db: Session,
    error: str | None = None,
    just_connected: bool = False,
    connected_as: str | None = None,
):
    """Every state-changing GOG settings route (save/disconnect) renders this
    same response: an out-of-band update to the card behind the modal (status
    badge, Connect/Disconnect) plus the modal's own content (paste-URL form,
    or a "Connected" confirmation) — mirrors _audible_response()'s identical
    shape below, for the same reason (one response shape means every action
    stays consistent regardless of which of the two ever triggered it).
    """
    context = _gog_context(db, error)
    context["just_connected"] = just_connected
    context["gog_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_gog_response.html", context)


def _get_or_create_audible_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_AUDIBLE)


def _audible_context(db: Session, error: str | None = None) -> dict:
    """audible_pending drives which of three states _audible_modal_content.html
    renders: not connected (plain locale form), a login in progress waiting
    on the pasted-back redirect URL (audible_pending set, see
    audible_connector.login_status), or connected/error (the normal
    status+disconnect view every other connector card uses).
    """
    cred = Credential.get(db, SOURCE_AUDIBLE)
    return {
        "audible_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "audible_error": error if error is not None else (cred.last_error if cred else None),
        "audible_pending": audible_connector.login_status(),
    }


def _oidc_context(request: Request, db: Session, oidc_error: str | None = None) -> dict:
    cfg = get_oidc_config(db) or {}
    return {
        "oidc_issuer": cfg.get("issuer", ""),
        "oidc_client_id": cfg.get("client_id", ""),
        "oidc_has_secret": bool(cfg.get("client_secret")),
        "oidc_enabled": cfg.get("enabled", False),
        "oidc_disable_password": cfg.get("disable_password", False),
        "oidc_error": oidc_error,
        "oidc_redirect_uri": str(request.url_for("oidc_callback")),
    }


def _backup_context(db: Session, backup_error: str | None = None) -> dict:
    cfg = backup.get_or_create_backup_settings(db)
    return {
        "backups_supported": backup.backups_supported(),
        "backup_enabled": cfg.enabled,
        "backup_daily_time_utc": cfg.daily_time_utc,
        "backup_retention_count": cfg.retention_count,
        "backup_last_backup_at": cfg.last_backup_at,
        "backup_error": backup_error,
        "backups": backup.list_backups(),
    }


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    # Each _*_context() below runs its own small query against `credential`
    # (a handful of rows, total, ever — one per source) rather than sharing
    # one bulk fetch. Deliberately not consolidated: this table's whole size
    # makes combining them save nothing measurable, and each context function
    # stays independently readable/testable and reusable by its own
    # save/disconnect routes below, which only ever need their one source.
    cred = _humble_credential(db)
    context = {
        "humble_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "humble_error": cred.last_error if cred else None,
    }
    context.update(_account_context(db))
    context.update(_oidc_context(request, db))
    context.update(_steam_context(db))
    context.update(_gog_context(db))
    context.update(_audible_context(db))
    context.update(_backup_context(db))
    return templates.TemplateResponse(request, "settings/index.html", context)


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
    # save_oidc's own "always persist what was typed" convention below rather
    # than an atomic all-or-nothing save.
    cfg = accounts.get_or_create_account_settings(db)
    cfg.email = email or None
    db.commit()

    if wants_password_change and not error:
        _set_password(new_password)

    return templates.TemplateResponse(request, "settings/_account_form.html", _account_context(db, error))


@router.post("/humble-key", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_humble_key(request: Request, session_key: str = Form(...), db: Session = Depends(get_db)):
    session_key = session_key.strip()
    result = await HumbleConnector({"session_key": session_key}).check_credentials()
    cred = _get_or_create_humble_credential(db)

    if result.ok:
        cred.encrypted_payload = encrypt_json({"session_key": session_key})
        cred.status = STATUS_OK
        cred.last_error = None
        db.commit()

        # humble-cli itself hardcodes this path/format (plaintext, 0600) — write it
        # directly so subprocess calls to the real binary authenticate without ever
        # invoking `humble-cli auth` interactively. See config.humble_cli_key_path
        # for why this must never point at a real ~/.humble-cli-key during dev.
        key_path = settings.humble_cli_key_path
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(session_key, encoding="utf-8")
        os.chmod(key_path, 0o600)
    else:
        cred.status = STATUS_ERROR
        cred.last_error = result.message
        db.commit()

    return templates.TemplateResponse(
        request, "settings/_humble_form.html", {"humble_status": cred.status, "humble_error": cred.last_error}
    )


@router.post("/oidc", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_oidc(
    request: Request,
    issuer: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    enabled: bool = Form(False),
    disable_password: bool = Form(False),
    db: Session = Depends(get_db),
):
    issuer, client_id, client_secret = issuer.strip(), client_id.strip(), client_secret.strip()
    cred = _get_or_create_oidc_credential(db)
    existing = get_oidc_config(db) or {}
    effective_secret = client_secret or existing.get("client_secret", "")

    error = None
    if enabled:
        if not issuer or not client_id or not effective_secret:
            error = "Issuer, Client ID, and Client Secret are all required to enable SSO."
        else:
            try:
                await discover(issuer)
            except Exception as exc:
                error = f"Could not verify issuer: {exc}"

    if disable_password and (error or not enabled):
        # Never let this request turn off the only login method that's actually verified —
        # this is the exact self-lockout this feature was built to avoid.
        disable_password = False
        error = error or "SSO must be enabled and verified before password login can be turned off."

    cred.encrypted_payload = encrypt_json(
        {
            "issuer": issuer,
            "client_id": client_id,
            "client_secret": effective_secret,
            "enabled": enabled and not error,
            "disable_password": disable_password,
        }
    )
    cred.status = STATUS_ERROR if error else (STATUS_OK if enabled else STATUS_NOT_CONFIGURED)
    cred.last_error = error
    db.commit()

    return templates.TemplateResponse(request, "settings/_oidc_form.html", _oidc_context(request, db, error))


@router.post("/steam", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_steam(request: Request, api_key: str = Form(""), steamid: str = Form(""), db: Session = Depends(get_db)):
    api_key = api_key.strip()
    steamid_input = steamid.strip()
    cred = _get_or_create_steam_credential(db)
    existing = decrypt_json(cred.encrypted_payload) if cred.encrypted_payload else {}
    effective_key = api_key or existing.get("api_key", "")
    effective_steamid_input = steamid_input or existing.get("steamid64", "")

    error = None
    connected_as = None
    resolved_steamid = existing.get("steamid64", "")
    if not effective_key or not effective_steamid_input:
        error = "API Key and SteamID (or profile URL) are both required."
    else:
        try:
            resolved_steamid = await steam_connector.resolve_steamid(effective_key, effective_steamid_input)
            result = await steam_connector.check_credentials(effective_key, resolved_steamid)
            if result.ok:
                connected_as = result.message
            else:
                error = result.message
        except Exception as exc:
            error = str(exc)

    cred.encrypted_payload = encrypt_json({"api_key": effective_key, "steamid64": resolved_steamid})
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    context = _steam_context(db)
    context["steam_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_steam_form.html", context)


@router.post("/humble/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_humble(request: Request, db: Session = Depends(get_db)):
    cred = _humble_credential(db)
    if cred:
        db.delete(cred)
        db.commit()

    # humble-cli reads this plaintext file directly (see save_humble_key above) —
    # leaving it behind after "disconnect" would mean the CLI subprocess (and
    # anyone with filesystem access) could still authenticate as this account.
    settings.humble_cli_key_path.unlink(missing_ok=True)

    return templates.TemplateResponse(
        request, "settings/_humble_form.html", {"humble_status": STATUS_NOT_CONFIGURED, "humble_error": None}
    )


@router.post("/steam/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_steam(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_STEAM)
    if cred:
        db.delete(cred)
        db.commit()
    return templates.TemplateResponse(request, "settings/_steam_form.html", _steam_context(db))


@router.post("/gog", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def save_gog(request: Request, pasted_code: str = Form(""), db: Session = Depends(get_db)):
    code = gog_connector.extract_code(pasted_code)

    error = None
    connected_as = None
    if not code:
        error = "Paste the URL or code you were redirected to after logging in."
    else:
        try:
            tokens = await gog_connector.exchange_code(code)
        except gog_connector.GogAuthError as exc:
            error = str(exc)
        else:
            gog_sync.save_refresh_token(db, tokens["refresh_token"])
            # Best-effort: GOG's real token response is documented (by the
            # reverse-engineering community, not GOG itself — see
            # gog_connector.py's own caveat) to include a numeric account
            # identifier alongside the tokens. Shown once, right after
            # connecting, as confirmation of which account just got linked —
            # the same role Steam's own check_credentials() message already
            # plays — degrading to no identity shown at all if it's ever
            # absent (e.g. demo mode's mock response) rather than guessing.
            connected_as = str(tokens["user_id"]) if tokens.get("user_id") else None

    cred = _get_or_create_gog_credential(db)
    cred.status = STATUS_ERROR if error else STATUS_OK
    cred.last_error = error
    db.commit()

    return _gog_response(request, db, error, just_connected=not error, connected_as=connected_as)


@router.post("/gog/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_gog(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_GOG)
    if cred:
        db.delete(cred)
        db.commit()
    return _gog_response(request, db)


def _audible_response(
    request: Request,
    db: Session,
    error: str | None = None,
    just_connected: bool = False,
    connected_as: str | None = None,
):
    """Every Audible settings route (login start/answer/status, disconnect)
    renders this same response: an out-of-band update to the card behind the
    modal (status badge, Connect/Disconnect buttons) plus the modal's own
    content (login form, a pending CAPTCHA/OTP/CVF/approval prompt, or a
    "Connected" confirmation) — see _audible_response.html. One shared
    response shape means every action stays consistent regardless of which
    of the two ever triggered it.
    """
    context = _audible_context(db, error)
    context["just_connected"] = just_connected
    context["audible_connected_as"] = connected_as
    return templates.TemplateResponse(request, "settings/_audible_response.html", context)


@router.post("/audible/login", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def start_audible_login(
    request: Request,
    locale: str = Form("us"),
    db: Session = Depends(get_db),
):
    locale = locale.strip().lower() or "us"

    error = None
    try:
        audible_connector.start_login(locale)
    except audible_connector.AudibleLoginError as exc:
        error = str(exc)

    return _audible_response(request, db, error)


@router.get("/audible/login/status", response_class=HTMLResponse)
def audible_login_status(request: Request, db: Session = Depends(get_db)):
    """Polled every 2s by _audible_modal_content.html while a login is in
    progress — same "poll a running background job" pattern
    bundles/_refresh_status.html already uses. Once login_result() has an
    answer, persists it (success or failure) and clears the pending state so
    this only ever fires once per login attempt.
    """
    just_connected = False
    connected_as = None
    result = audible_connector.login_result()
    if result is not None:
        auth, error = result
        if auth is not None:
            audible_sync.save_authenticator(db, auth)
            cred = Credential.get(db, SOURCE_AUDIBLE)
            cred.status = STATUS_OK
            cred.last_error = None
            just_connected = True
            # Best-effort: Amazon's device-registration response includes a
            # "customer_info" extension (confirmed present as a real
            # Authenticator attribute, requested by audible/register.py) —
            # its exact fields are unconfirmed against a real account from
            # this sandbox, so this degrades to no identity shown at all
            # rather than guessing further if it's ever absent/differently
            # shaped, the same philosophy audible_connector.py's own
            # best-effort field parsing already uses.
            customer_info = getattr(auth, "customer_info", None) or {}
            connected_as = customer_info.get("name") or customer_info.get("user_id")
        else:
            cred = _get_or_create_audible_credential(db)
            cred.status = STATUS_ERROR
            cred.last_error = error
        db.commit()
        audible_connector.clear_pending()

    return _audible_response(request, db, just_connected=just_connected, connected_as=connected_as)


@router.post("/audible/login/answer", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def answer_audible_login(request: Request, pasted_url: str = Form(""), db: Session = Depends(get_db)):
    audible_connector.answer_login(pasted_url.strip())
    return _audible_response(request, db)


@router.post("/audible/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_audible(request: Request, db: Session = Depends(get_db)):
    cred = Credential.get(db, SOURCE_AUDIBLE)
    if cred:
        db.delete(cred)
        db.commit()
    audible_connector.clear_pending()
    return _audible_response(request, db)


@router.post("/backups/config", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def save_backup_config(
    request: Request,
    enabled: bool = Form(False),
    daily_time_utc: str = Form("03:00"),
    retention_count: int = Form(7),
    db: Session = Depends(get_db),
):
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", _backup_context(db)
        )
    error = None
    if not _TIME_PATTERN.match(daily_time_utc):
        error = "Time must be in 24-hour HH:MM format, e.g. 03:00."
    elif retention_count < 1:
        error = "Keep at least 1 backup."
    else:
        cfg = backup.get_or_create_backup_settings(db)
        cfg.enabled = enabled
        cfg.daily_time_utc = daily_time_utc
        cfg.retention_count = retention_count
        db.commit()

    return templates.TemplateResponse(request, "settings/_backup_form.html", _backup_context(db, error))


@router.post("/backups/run", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def run_backup_now(request: Request, db: Session = Depends(get_db)):
    # Required, not cosmetic: create_backup() -> db_file_path() now raises
    # RuntimeError on a non-SQLite backend, which the `except OSError` below
    # does not catch — without this guard a stray call here would 500
    # instead of degrading cleanly.
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", _backup_context(db)
        )
    error = None
    try:
        backup.create_backup(db)
    except OSError as exc:
        error = f"Backup failed: {exc}"
    return templates.TemplateResponse(request, "settings/_backup_form.html", _backup_context(db, error))


@router.get("/backups/{filename}/download")
def download_backup(filename: str):
    # Checked against the real directory listing rather than sanitized-and-joined
    # — no path-traversal surface at all since the value must already be one of
    # the files actually present, never a caller-controlled path.
    if filename not in backup.valid_backup_names():
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(backup.backups_dir() / filename, filename=filename, media_type="application/octet-stream")


@router.post("/backups/{filename}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def delete_backup_route(request: Request, filename: str, db: Session = Depends(get_db)):
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", _backup_context(db)
        )
    error = None if backup.delete_backup(filename) else "Backup not found"
    return templates.TemplateResponse(request, "settings/_backup_form.html", _backup_context(db, error))


@router.post("/backups/restore", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
async def restore_backup_route(request: Request, backup_file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not backup.backups_supported():
        return templates.TemplateResponse(
            request, "settings/_backup_form.html", _backup_context(db)
        )
    error = None
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "upload.db"
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(backup_file.file, f)

        try:
            backup.restore_backup(db, tmp_path)
        except backup.InvalidBackupFile as exc:
            error = str(exc)
        except Exception as exc:
            error = f"Restore failed: {exc}"

    if error:
        return templates.TemplateResponse(request, "settings/_backup_form.html", _backup_context(db, error))

    # db's connection may still be attached to the just-replaced file's old,
    # now-orphaned inode (see restore_backup's own docstring) — read the
    # freshly-restored state back with a brand new session rather than reusing it.
    fresh_db = SessionLocal()
    try:
        context = _backup_context(fresh_db)
    finally:
        fresh_db.close()
    return templates.TemplateResponse(request, "settings/_backup_form.html", context)


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
