"""Settings page: one router per connected integration, aggregated here
under the shared /settings prefix. Used to be a single ~900-line
settings.py — split by integration (account/oidc/humble/steam/gog/audible/
chirp/amazon_music/backups/api_tokens/logs) since each one's own save/
disconnect/probe routes never call into any other's, and the next
integration's settings should be its own file, not another few hundred
lines bolted onto whichever file happened to be biggest.

HumbleConnector/chirp_connector/gog_connector/steam_connector/settings are
re-exported below for no code in this package — only so that existing
`unittest.mock.patch("app.routers.settings.X...")` call sites (several
pre-dating this split) keep resolving. mock.patch looks up `X` as an
attribute of whatever module `app.routers.settings` already is; since
Python modules and classes are the same object everywhere they're
imported, patching through this package's own namespace still reaches
the real one each submodule below actually calls.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings  # noqa: F401 — re-exported, see module docstring
from app.connectors import chirp_connector, gog_connector, steam_connector  # noqa: F401 — re-exported, see module docstring
from app.connectors.humble_connector import HumbleConnector  # noqa: F401 — re-exported, see module docstring
from app.deps import get_db
from app.models.credential import STATUS_NOT_CONFIGURED
from app.templates_env import templates

from . import account, amazon_music, api_tokens, audible, backups, chirp, gog, humble, logs, oidc, steam
from .deployment import deployment_context
from .humble import humble_credential

router = APIRouter(prefix="/settings")
for _sub in (account, humble, oidc, steam, gog, audible, chirp, amazon_music, backups, api_tokens, logs):
    router.include_router(_sub.router)


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    # Each _*_context() below runs its own small query against `credential`
    # (a handful of rows, total, ever — one per source) rather than sharing
    # one bulk fetch. Deliberately not consolidated: this table's whole size
    # makes combining them save nothing measurable, and each context function
    # stays independently readable/testable and reusable by its own
    # save/disconnect routes below, which only ever need their one source.
    cred = humble_credential(db)
    context = {
        "humble_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "humble_error": cred.last_error if cred else None,
    }
    context.update(account.account_context(db))
    context.update(oidc.oidc_context(request, db))
    context.update(steam.steam_context(db))
    context.update(gog.gog_context(db))
    context.update(audible.audible_context(db))
    context.update(chirp.chirp_context(db))
    context.update(backups.backup_context(db))
    context.update(amazon_music.amazon_music_context(db))
    context.update(deployment_context(request))
    context.update(api_tokens.api_context(db))
    return templates.TemplateResponse(request, "settings/index.html", context)
