import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.connectors.humble_connector import order_page_url
from app.csrf import require_csrf
from app.deps import get_db
from app.entitlement_status import owned_title_sets, parse_expiration
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_GOG
from app.models.gog_game import GogGame
from app.ratelimit import RateLimiter, rate_limit
from app.sync import gog_sync
from app.templates_env import templates

router = APIRouter(prefix="/gog")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _unredeemed_rows(db: Session) -> list[dict]:
    # gog_owned.is_(False) alone is the right filter — it's set by either the
    # gog_id or the name-matching path in sync/gog_sync.py, so requiring
    # gog_id here too would silently hide every name-matched row.
    rows = (
        db.query(BundleEntitlement, Bundle)
        .join(Bundle, Bundle.gamekey == BundleEntitlement.gamekey)
        .filter(BundleEntitlement.gog_owned.is_(False))
        .order_by(Bundle.name)
        .all()
    )
    # gog_owned above already covers name-based matching in the common case
    # (see sync/gog_sync.py — a real gog_id is almost never populated), so
    # owned_gog_titles mostly only adds something new for the rare row that
    # *does* carry a gog_id and got exact-ID-matched instead. owned_steam_titles
    # is the genuinely new signal: the same game entirely, owned via Steam.
    owned_steam_titles, owned_gog_titles = owned_title_sets(db)

    result = []
    for ent, bundle in rows:
        try:
            raw = json.loads(ent.raw_json) if ent.raw_json else {}
        except (ValueError, TypeError):
            raw = {}
        expires_at = parse_expiration(raw)
        title = ent.key_name.strip().casefold()
        result.append(
            {
                "key_name": ent.key_name,
                "gamekey": ent.gamekey,
                "bundle_name": bundle.name,
                "redeemed_on_humble": ent.redeemed_on_humble,
                "redeem_url": order_page_url(ent.gamekey),
                "owned_as_different_gog_listing": title in owned_gog_titles,
                "owned_on_steam": title in owned_steam_titles,
                "expires_at": expires_at,
                "days_until_expired": (expires_at - datetime.now(timezone.utc)).days if expires_at else None,
                "is_expired": expires_at is not None and expires_at < datetime.now(timezone.utc),
            }
        )
    return result


def _context(db: Session) -> dict:
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    games = db.query(GogGame).order_by(GogGame.title).all()
    checked_count = db.query(BundleEntitlement).filter(BundleEntitlement.gog_owned.isnot(None)).count()
    return {
        "gog_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "gog_error": cred.last_error if cred else None,
        "games": games,
        "game_count": len(games),
        "checked_entitlement_count": checked_count,
        "unredeemed": _unredeemed_rows(db),
        "last_synced": db.query(func.max(GogGame.fetched_at)).scalar(),
    }


@router.get("", response_class=HTMLResponse)
def gog_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "gog/list.html", _context(db))


@router.post("/refresh", response_class=HTMLResponse, dependencies=[Depends(rate_limit(_refresh_limiter, "gog-refresh")), Depends(require_csrf)])
async def refresh_gog(request: Request, db: Session = Depends(get_db)):
    try:
        await gog_sync.refresh_gog_library(db)
    except gog_sync.NotConnectedError:
        pass
    except Exception:
        pass  # error already recorded on the credential row by refresh_gog_library
    return templates.TemplateResponse(request, "gog/_content.html", _context(db))
