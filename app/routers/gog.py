from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.connectors import gog_connector
from app.csrf import require_csrf
from app.deps import get_db
from app.entitlement_status import unredeemed_rows
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_GOG
from app.models.gog_game import GogGame
from app.ratelimit import RateLimiter, rate_limit
from app.sync import gog_sync
from app.templates_env import templates

router = APIRouter(prefix="/gog")
_refresh_limiter = RateLimiter(max_calls=5, period_seconds=60)


def _context(db: Session) -> dict:
    cred = Credential.get(db, SOURCE_GOG)
    # GogGame holds every product type the API returns (games and movies
    # both — see its own docstring); ordered content_type first so movies
    # group together rather than interleaving with games alphabetically.
    items = db.query(GogGame).order_by(GogGame.content_type, GogGame.title).all()
    checked_count = db.query(BundleEntitlement).filter(BundleEntitlement.gog_owned.isnot(None)).count()
    return {
        "gog_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "gog_error": cred.last_error if cred else None,
        "games": items,
        "game_count": sum(1 for i in items if i.content_type == gog_connector.CONTENT_TYPE_GAME),
        "movie_count": sum(1 for i in items if i.content_type == gog_connector.CONTENT_TYPE_MOVIE),
        "checked_entitlement_count": checked_count,
        "unredeemed": unredeemed_rows(db, "gog"),
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
