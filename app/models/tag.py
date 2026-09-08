from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Tag(Base):
    """A user-defined label. One shared namespace across both taggable kinds
    (bundles and catalog items) rather than two separate tag pools — one
    management page, one place to rename/delete something everywhere it's used.
    """

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class BundleTag(Base):
    """gamekey, not an ORM relationship() — mirrors how Download/
    BundleEntitlement already reference Bundle.gamekey directly elsewhere in
    this app rather than through a relationship.
    """

    __tablename__ = "bundle_tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"), nullable=False)
    gamekey: Mapped[str] = mapped_column(ForeignKey("bundle.gamekey"), nullable=False)

    __table_args__ = (UniqueConstraint("tag_id", "gamekey", name="uq_bundle_tag"),)


class ItemTag(Base):
    """machine_name, not a foreign key to an items table — catalog items have
    no persisted table at all (Catalog is computed fresh from
    Bundle.raw_json every request, see routers/catalog.py), so machine_name
    (the same cross-bundle identity key Catalog already keys everything on)
    is the only stable thing available to attach a tag to.
    """

    __tablename__ = "item_tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"), nullable=False)
    machine_name: Mapped[str] = mapped_column(String(300), nullable=False)

    __table_args__ = (UniqueConstraint("tag_id", "machine_name", name="uq_item_tag"),)
