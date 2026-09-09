from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DownloadDestination(Base):
    """A relocation rule: where a completed download's file should move to,
    based on its format and (optionally) one of its tags. Resolution precedence
    lives in app/downloads/relocate.py, not here — this is just storage.

    `formats` is a comma-separated, case-insensitive list of Download.file_format
    values; empty means "matches any format", which is what lets one row in this
    same table also serve as the catch-all default destination — no separate
    "default location" setting needed.

    `tag_id` nullable: NULL means format-only (or, combined with empty formats,
    the default rule); set means this rule only applies to items carrying that
    tag (bundle-level or item-level — see relocate.py), which is how a format
    that's ambiguous on its own (e.g. PDF: comics vs. regular books) gets routed
    correctly.
    """

    __tablename__ = "download_destination"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    formats: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tag_id: Mapped[int | None] = mapped_column(ForeignKey("tag.id"), nullable=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
