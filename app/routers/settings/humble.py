from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import humble_key
from app.config import settings
from app.connectors.humble_connector import HumbleConnector
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_HUMBLE, STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.security import encrypt_json
from app.templates_env import templates

router = APIRouter()


def humble_credential(db: Session) -> Credential | None:
    return Credential.get(db, SOURCE_HUMBLE)


def _get_or_create_humble_credential(db: Session) -> Credential:
    return Credential.get_or_create(db, SOURCE_HUMBLE)


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

        # humble-cli itself hardcodes this path/format (plaintext, 0600) — written
        # directly so subprocess calls to the real binary authenticate without ever
        # invoking `humble-cli auth` interactively. Shared with the startup
        # self-heal in app/humble_key.py, which rewrites this same file after a
        # container recreation wipes it.
        humble_key.write_key_file(session_key)
    else:
        cred.status = STATUS_ERROR
        cred.last_error = result.message
        db.commit()

    return templates.TemplateResponse(
        request,
        "settings/_humble_form.html",
        {"humble_status": cred.status, "humble_error": cred.last_error, "humble_saved": result.ok},
    )


@router.post("/humble/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def disconnect_humble(request: Request, db: Session = Depends(get_db)):
    cred = humble_credential(db)
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
