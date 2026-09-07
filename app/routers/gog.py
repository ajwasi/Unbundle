from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors.humble_connector import order_page_url
from app.deps import get_db
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_GOG
from app.models.gog_game import GogGame
from app.sync import gog_sync
from app.templates_env import templates

router = APIRouter(prefix="/gog")


def _unredeemed_rows(db: Session) -> list[dict]:
    rows = (
        db.query(BundleEntitlement, Bundle)
        .join(Bundle, Bundle.gamekey == BundleEntitlement.gamekey)
        .filter(BundleEntitlement.gog_id.isnot(None), BundleEntitlement.gog_owned.is_(False))
        .order_by(Bundle.name)
        .all()
    )
    return [
        {
            "key_name": ent.key_name,
            "gamekey": ent.gamekey,
            "bundle_name": bundle.name,
            "redeemed_on_humble": ent.redeemed_on_humble,
            "redeem_url": order_page_url(ent.gamekey),
        }
        for ent, bundle in rows
    ]


def _context(db: Session) -> dict:
    cred = db.query(Credential).filter(Credential.source == SOURCE_GOG).one_or_none()
    games = db.query(GogGame).order_by(GogGame.title).all()
    checked_count = db.query(BundleEntitlement).filter(BundleEntitlement.gog_id.isnot(None)).count()
    return {
        "gog_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "gog_error": cred.last_error if cred else None,
        "games": games,
        "game_count": len(games),
        "checked_entitlement_count": checked_count,
        "unredeemed": _unredeemed_rows(db),
    }


@router.get("", response_class=HTMLResponse)
def gog_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "gog/list.html", _context(db))


@router.post("/refresh", response_class=HTMLResponse)
async def refresh_gog(request: Request, db: Session = Depends(get_db)):
    try:
        await gog_sync.refresh_gog_library(db)
    except gog_sync.NotConnectedError:
        pass
    except Exception:
        pass  # error already recorded on the credential row by refresh_gog_library
    return templates.TemplateResponse(request, "gog/_content.html", _context(db))
