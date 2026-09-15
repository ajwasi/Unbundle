from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.entitlement_status import unredeemed_rows
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_STEAM
from app.models.steam_game import SteamGame
from app.ratelimit import RateLimiter, rate_limit
from app.sync import steam_sync
from app.templates_env import templates

router = APIRouter(prefix="/steam")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _context(db: Session) -> dict:
    cred = Credential.get(db, SOURCE_STEAM)
    games = db.query(SteamGame).order_by(SteamGame.name).all()
    checked_count = db.query(BundleEntitlement).filter(BundleEntitlement.steam_app_id.isnot(None)).count()
    return {
        "steam_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "steam_error": cred.last_error if cred else None,
        "games": games,
        "game_count": len(games),
        "total_playtime_hours": round(sum(g.playtime_forever_minutes for g in games) / 60, 1),
        "checked_entitlement_count": checked_count,
        "unredeemed": unredeemed_rows(db, "steam"),
        "last_synced": db.query(func.max(SteamGame.fetched_at)).scalar(),
    }


@router.get("", response_class=HTMLResponse)
def steam_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "steam/list.html", _context(db))


@router.post("/refresh", response_class=HTMLResponse, dependencies=[Depends(rate_limit(_refresh_limiter, "steam-refresh")), Depends(require_csrf)])
async def refresh_steam(request: Request, db: Session = Depends(get_db)):
    try:
        await steam_sync.refresh_steam_library(db)
    except steam_sync.NotConnectedError:
        pass  # nothing to fetch — the page already shows a "not configured" state
    except Exception:
        pass  # error already recorded on the credential row by refresh_steam_library
    return templates.TemplateResponse(request, "steam/_content.html", _context(db))
