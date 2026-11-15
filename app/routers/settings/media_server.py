"""Settings routes for a "media server presence" integration.

AudiobookShelf, Plex and Navidrome all get the same three routes — save
(verify the credential, then store it encrypted), refresh (re-cache what the
server holds), disconnect — differing only in their form fields, their
connector's `check_credentials` arguments, and their labels. Each integration
is described once as a `MediaServerSpec` and `register_media_server()` adds
the routes, so a fourth server is a short spec rather than another 90-line
copy.

Credentials are always stored/read through `Credential` + `encrypt_json` /
`decrypt_json`; a secret field is never echoed back to the template — the
context only says whether one is saved (`<name>_has_<token|password>`).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import STATUS_ERROR, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json
from app.templates_env import templates


@dataclass(frozen=True)
class Field:
    key: str  # form field name and key inside the stored payload
    secret: bool = False  # never sent back to the page; the context only reports has_<has_key>
    has_key: str = ""  # name of that flag (e.g. "token" -> <name>_has_token)
    required: bool = False
    keep_when_blank: bool = True  # blank submission keeps the saved value (False: blank clears it)
    strip: bool = True


@dataclass(frozen=True)
class MediaServerSpec:
    name: str  # URL segment (/settings/<name>) and context-key prefix
    source: str  # Credential SOURCE_* constant
    connector: ModuleType  # needs check_credentials(*[effective value of each check_args key])
    check_args: tuple[str, ...]
    fields: tuple[Field, ...]
    required_message: str
    refresh: Callable[[Session], Awaitable[int]]  # re-caches the server's contents, returns the count


def register_media_server(router: APIRouter, spec: MediaServerSpec) -> Callable[..., dict]:
    """Adds the save / refresh / disconnect routes for `spec` to `router`.
    Returns the integration's context function (what the Settings page and
    its own form template are rendered from)."""
    name = spec.name
    template = f"settings/_{name}_form.html"

    def context(db: Session, error: str | None = None) -> dict:
        cred = Credential.get(db, spec.source)
        payload = decrypt_json(cred.encrypted_payload) if cred and cred.encrypted_payload else {}
        ctx = {
            f"{name}_status": cred.status if cred else STATUS_NOT_CONFIGURED,
            f"{name}_error": error if error is not None else (cred.last_error if cred else None),
        }
        for field in spec.fields:
            if field.secret:
                ctx[f"{name}_has_{field.has_key}"] = bool(payload.get(field.key))
            else:
                ctx[f"{name}_{field.key}"] = payload.get(field.key, "")
        return ctx

    @router.post(f"/{name}", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
    async def save(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        cred = Credential.get_or_create(db, spec.source)
        existing = decrypt_json(cred.encrypted_payload) if cred.encrypted_payload else {}

        effective: dict[str, str] = {}
        for field in spec.fields:
            submitted = str(form.get(field.key) or "")
            if field.strip:
                submitted = submitted.strip()
            effective[field.key] = (submitted or existing.get(field.key, "")) if field.keep_when_blank else submitted

        error = None
        connected_as = None
        if any(not effective[f.key] for f in spec.fields if f.required):
            error = spec.required_message
        else:
            result = await spec.connector.check_credentials(*(effective[k] for k in spec.check_args))
            if result.ok:
                connected_as = result.message
            else:
                error = result.message

        cred.encrypted_payload = encrypt_json(effective)
        cred.status = STATUS_ERROR if error else STATUS_OK
        cred.last_error = error
        db.commit()

        ctx = context(db)
        ctx[f"{name}_connected_as"] = connected_as
        return templates.TemplateResponse(request, template, ctx)

    @router.post(f"/{name}/refresh", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
    async def refresh(request: Request, db: Session = Depends(get_db)):
        error = None
        count = None
        try:
            count = await spec.refresh(db)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as an error message
            error = str(exc)

        ctx = context(db, error=error)
        ctx[f"{name}_refreshed_count"] = count
        return templates.TemplateResponse(request, template, ctx)

    @router.post(f"/{name}/disconnect", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
    def disconnect(request: Request, db: Session = Depends(get_db)):
        cred = Credential.get(db, spec.source)
        if cred:
            db.delete(cred)
            db.commit()
        return templates.TemplateResponse(request, template, context(db))

    return context
