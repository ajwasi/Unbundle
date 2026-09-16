from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.entitlement_status import unredeemed_rows
from app.list_views import render_list_or_partial, sorted_query
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_STEAM
from app.models.steam_game import SteamGame
from app.ratelimit import RateLimiter, rate_limit
from app.sync import steam_sync
from app.templates_env import templates

router = APIRouter(prefix="/steam")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

_SORT_COLUMNS = {
    "name": SteamGame.name,
    "playtime": SteamGame.playtime_forever_minutes,
}


def _context(db: Session, q: str = "", sort: str = "name", dir: str = "asc") -> dict:
    cred = Credential.get(db, SOURCE_STEAM)
    query = db.query(SteamGame)
    if q:
        query = query.filter(SteamGame.name.ilike(f"%{q}%"))
    games = sorted_query(query, _SORT_COLUMNS, sort, dir, SteamGame.name).all()

    # Library-wide totals, independent of the current search filter — same
    # "stable overview vs. filtered table" split bundles/list.html's
    # grand_total_spent (vs. the table's own filtered_total_spent) already
    # uses. A separate aggregate query rather than re-fetching every row.
    total_game_count, total_playtime_minutes = db.query(
        func.count(SteamGame.appid), func.coalesce(func.sum(SteamGame.playtime_forever_minutes), 0)
    ).one()
    checked_count = db.query(BundleEntitlement).filter(BundleEntitlement.steam_app_id.isnot(None)).count()
    return {
        "steam_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "steam_error": cred.last_error if cred else None,
        "games": games,
        "q": q,
        "sort": sort,
        "dir": dir,
        "game_count": total_game_count,
        "total_playtime_hours": round(total_playtime_minutes / 60, 1),
        "checked_entitlement_count": checked_count,
        "unredeemed": unredeemed_rows(db, "steam"),
        "last_synced": db.query(func.max(SteamGame.fetched_at)).scalar(),
    }


@router.get("", response_class=HTMLResponse)
def steam_page(request: Request, q: str = "", sort: str = "name", dir: str = "asc", db: Session = Depends(get_db)):
    context = _context(db, q, sort, dir)
    return render_list_or_partial(request, templates, "steam/list.html", "steam/_games_table.html", context)


@router.post("/refresh", response_class=HTMLResponse, dependencies=[Depends(rate_limit(_refresh_limiter, "steam-refresh")), Depends(require_csrf)])
async def refresh_steam(request: Request, db: Session = Depends(get_db)):
    try:
        await steam_sync.refresh_steam_library(db)
    except steam_sync.NotConnectedError:
        pass  # nothing to fetch — the page already shows a "not configured" state
    except Exception:
        pass  # error already recorded on the credential row by refresh_steam_library
    return templates.TemplateResponse(request, "steam/_content.html", _context(db))
