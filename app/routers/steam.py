from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.connectors.humble_connector import order_page_url
from app.deps import get_db
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import STATUS_NOT_CONFIGURED, Credential, SOURCE_STEAM
from app.models.steam_game import SteamGame
from app.sync import steam_sync
from app.templates_env import templates

router = APIRouter(prefix="/steam")


def _unredeemed_rows(db: Session) -> list[dict]:
    rows = (
        db.query(BundleEntitlement, Bundle)
        .join(Bundle, Bundle.gamekey == BundleEntitlement.gamekey)
        .filter(BundleEntitlement.steam_app_id.isnot(None), BundleEntitlement.steam_owned.is_(False))
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
    cred = db.query(Credential).filter(Credential.source == SOURCE_STEAM).one_or_none()
    games = db.query(SteamGame).order_by(SteamGame.name).all()
    checked_count = db.query(BundleEntitlement).filter(BundleEntitlement.steam_app_id.isnot(None)).count()
    return {
        "steam_status": cred.status if cred else STATUS_NOT_CONFIGURED,
        "steam_error": cred.last_error if cred else None,
        "games": games,
        "game_count": len(games),
        "total_playtime_hours": round(sum(g.playtime_forever_minutes for g in games) / 60, 1),
        "checked_entitlement_count": checked_count,
        "unredeemed": _unredeemed_rows(db),
    }


@router.get("", response_class=HTMLResponse)
def steam_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "steam/list.html", _context(db))


@router.post("/refresh", response_class=HTMLResponse)
async def refresh_steam(request: Request, db: Session = Depends(get_db)):
    try:
        await steam_sync.refresh_steam_library(db)
    except steam_sync.NotConnectedError:
        pass  # nothing to fetch — the page already shows a "not configured" state
    except Exception:
        pass  # error already recorded on the credential row by refresh_steam_library
    return templates.TemplateResponse(request, "steam/_content.html", _context(db))
