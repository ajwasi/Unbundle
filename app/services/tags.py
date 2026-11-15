"""Tag helpers shared by the Tags, Bundles and Catalog routers."""

from sqlalchemy.orm import Session

from app.models.tag import Tag


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
