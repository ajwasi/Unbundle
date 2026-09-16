from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Session

from app.connectors import gog_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.entitlement_status import unredeemed_rows
from app.list_views import render_list_or_partial
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_GOG
from app.models.gog_game import GogGame
from app.ratelimit import RateLimiter, rate_limit
from app.sync import gog_sync
from app.templates_env import templates

router = APIRouter(prefix="/gog")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)

_SORT_COLUMNS = {
    "title": GogGame.title,
    "type": GogGame.content_type,
}


def _context(db: Session, q: str = "", content_type: str = "", sort: str = "title", dir: str = "asc") -> dict:
    cred = Credential.get(db, SOURCE_GOG)
    query = db.query(GogGame)
    if q:
        query = query.filter(GogGame.title.ilike(f"%{q}%"))
    if content_type:
        query = query.filter(GogGame.content_type == content_type)
    if sort == "type":
        # Ties within a content_type still sort by title, matching the
        # unfiltered default ordering below (content_type first, title
        # second) rather than falling back to whatever order SQLite hands
        # back for equal keys.
        column_order = [desc(GogGame.content_type) if dir == "desc" else asc(GogGame.content_type), GogGame.title]
    else:
        column_order = [desc(GogGame.title) if dir == "desc" else asc(GogGame.title)]
    items = query.order_by(*column_order).all()

    # Library-wide totals, independent of the current search/type filter —
    # same "stable overview vs. filtered table" split bundles/list.html's
    # grand_total_spent (vs. the table's own filtered_total_spent) already
    # uses. One grouped aggregate rather than a second full-row fetch.
    counts_by_type = dict(db.query(GogGame.content_type, func.count(GogGame.product_id)).group_by(GogGame.content_type).all())
    checked_count = db.query(BundleEntitlement).filter(BundleEntitlement.gog_owned.isnot(None)).count()
    return {
        "gog_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "gog_error": cred.last_error if cred else None,
        "games": items,
        "q": q,
        "content_type": content_type,
        "sort": sort,
        "dir": dir,
        "total_count": sum(counts_by_type.values()),
        "game_count": counts_by_type.get(gog_connector.CONTENT_TYPE_GAME, 0),
        "movie_count": counts_by_type.get(gog_connector.CONTENT_TYPE_MOVIE, 0),
        "checked_entitlement_count": checked_count,
        "unredeemed": unredeemed_rows(db, "gog"),
        "last_synced": db.query(func.max(GogGame.fetched_at)).scalar(),
    }


@router.get("", response_class=HTMLResponse)
def gog_page(
    request: Request,
    q: str = "",
    content_type: str = "",
    sort: str = "title",
    dir: str = "asc",
    db: Session = Depends(get_db),
):
    context = _context(db, q, content_type, sort, dir)
    return render_list_or_partial(request, templates, "gog/list.html", "gog/_games_table.html", context)


@router.post("/refresh", response_class=HTMLResponse, dependencies=[Depends(rate_limit(_refresh_limiter, "gog-refresh")), Depends(require_csrf)])
async def refresh_gog(request: Request, db: Session = Depends(get_db)):
    try:
        await gog_sync.refresh_gog_library(db)
    except gog_sync.NotConnectedError:
        pass
    except Exception:
        pass  # error already recorded on the credential row by refresh_gog_library
    return templates.TemplateResponse(request, "gog/_content.html", _context(db))
