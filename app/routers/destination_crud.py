"""Shared CRUD for a "list of folders + one flagged default" destination
feature — AudibleDownloadDestination and AmazonMusicDestination are
identical shapes (see either model's own docstring for why: one content
type each, nothing to route by, so there's nothing a format/tag-based rule
table like DownloadDestination would add). This used to be two ~90%-
identical copies of the same six functions in routers/audible.py and
routers/amazon_music.py; downloads.py's own destination CRUD is NOT this —
it's a genuinely different shape (format/tag routing rules, no is_default)
and stays bespoke.

A future store that just needs "pick a folder, flag one default" calls
register_simple_destination_routes() once and is done — no new router
functions, model boilerplate (beyond the model itself), or template.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.templates_env import templates

_TEMPLATE = "_simple_destinations.html"


def destinations_context(
    db: Session,
    model,
    *,
    url_prefix: str,
    dom_id: str,
    folder_label: str,
    name_placeholder: str,
    path_placeholder: str,
) -> dict:
    """Context for _TEMPLATE — also called directly by a store's own main
    page context builder (not just the CRUD routes below), so the
    destinations list is always built the same one way.
    """
    return {
        "destinations": [
            {"id": d.id, "name": d.name, "path": d.path, "is_default": d.is_default}
            for d in db.query(model).order_by(model.id).all()
        ],
        "destinations_url_prefix": url_prefix,
        "destinations_dom_id": dom_id,
        "destinations_folder_label": folder_label,
        "destinations_name_placeholder": name_placeholder,
        "destinations_path_placeholder": path_placeholder,
    }


def register_simple_destination_routes(
    router: APIRouter,
    model,
    *,
    url_prefix: str,
    dom_id: str,
    folder_label: str,
    name_placeholder: str,
    path_placeholder: str,
) -> None:
    """Mounts POST {router.prefix}/destinations, .../{id}/delete, and
    .../{id}/set-default onto `router`, backed by `model`. The kwargs are
    exactly destinations_context's own — kept as one dict by the caller
    (see audible.py/amazon_music.py) so the main page context builder and
    these routes can never pass different values for the same store.
    """

    def _ctx(db: Session) -> dict:
        return destinations_context(
            db,
            model,
            url_prefix=url_prefix,
            dom_id=dom_id,
            folder_label=folder_label,
            name_placeholder=name_placeholder,
            path_placeholder=path_placeholder,
        )

    @router.post("/destinations", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
    def create_destination(request: Request, name: str = Form(default=""), path: str = Form(default=""), db: Session = Depends(get_db)):
        if name.strip() and path.strip():
            # The first destination anyone adds becomes the default automatically
            # — otherwise a user who configures exactly one directory would still
            # need a second click to actually use it.
            is_first = db.query(model).count() == 0
            db.add(model(name=name.strip(), path=path.strip(), is_default=is_first))
            db.commit()
        return templates.TemplateResponse(request, _TEMPLATE, _ctx(db))

    @router.post("/destinations/{destination_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
    def delete_destination(request: Request, destination_id: int, db: Session = Depends(get_db)):
        dest = db.get(model, destination_id)
        if dest is not None:
            db.delete(dest)
            db.commit()
        return templates.TemplateResponse(request, _TEMPLATE, _ctx(db))

    @router.post("/destinations/{destination_id}/set-default", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
    def set_default_destination(request: Request, destination_id: int, db: Session = Depends(get_db)):
        dest = db.get(model, destination_id)
        if dest is not None:
            db.query(model).update({"is_default": False})
            dest.is_default = True
            db.commit()
        return templates.TemplateResponse(request, _TEMPLATE, _ctx(db))
