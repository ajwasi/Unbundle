"""Tag management page — create/rename/delete a Tag itself. Attaching/removing
a tag on a specific bundle or catalog item lives in bundles.py/catalog.py
instead (co-located with everything else about that resource), consistent
with this app's existing per-page router split.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.csrf import require_csrf
from app.deps import get_db
from app.models.tag import BundleTag, ItemTag, Tag
from app.templates_env import templates

router = APIRouter(prefix="/tags")


def get_or_create_tag(db: Session, name: str) -> Tag:
    """Shared with bundles.py/catalog.py's "add tag" endpoints — tagging
    something with a name that doesn't exist yet creates it, the same
    create-on-first-use convention most tagging UIs use rather than requiring
    a separate "create the tag first" step.
    """
    name = name.strip()
    tag = db.query(Tag).filter(Tag.name == name).one_or_none()
    if tag is None:
        tag = Tag(name=name)
        db.add(tag)
        db.flush()
    return tag


def _tag_rows(db: Session) -> list[dict]:
    tags = db.query(Tag).order_by(Tag.name).all()
    bundle_counts = dict(db.query(BundleTag.tag_id, func.count(BundleTag.id)).group_by(BundleTag.tag_id).all())
    item_counts = dict(db.query(ItemTag.tag_id, func.count(ItemTag.id)).group_by(ItemTag.tag_id).all())
    return [
        {
            "id": t.id,
            "name": t.name,
            "bundle_count": bundle_counts.get(t.id, 0),
            "item_count": item_counts.get(t.id, 0),
        }
        for t in tags
    ]


@router.get("", response_class=HTMLResponse)
def list_tags(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "tags/list.html", {"tags": _tag_rows(db)})


@router.post("", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def create_tag(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    if name.strip():
        get_or_create_tag(db, name)
        db.commit()
    return templates.TemplateResponse(request, "tags/_table.html", {"tags": _tag_rows(db)})


@router.post("/{tag_id}/rename", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def rename_tag(request: Request, tag_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    tag = db.get(Tag, tag_id)
    name = name.strip()
    if tag is not None and name:
        tag.name = name
        db.commit()
    return templates.TemplateResponse(request, "tags/_table.html", {"tags": _tag_rows(db)})


@router.post("/{tag_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def delete_tag(request: Request, tag_id: int, db: Session = Depends(get_db)):
    tag = db.get(Tag, tag_id)
    if tag is not None:
        db.query(BundleTag).filter(BundleTag.tag_id == tag_id).delete()
        db.query(ItemTag).filter(ItemTag.tag_id == tag_id).delete()
        db.delete(tag)
        db.commit()
    return templates.TemplateResponse(request, "tags/_table.html", {"tags": _tag_rows(db)})
