from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudibleDownloadDestination(Base):
    """A named directory downloaded audiobooks (.m4b) can be written into.

    Same deliberately-simple shape as AmazonMusicDestination (see its own
    docstring for why this doesn't reuse Humble's format/tag-routing
    DownloadDestination): one content type, nothing to route by beyond "the
    one the user flagged as default". Scoped to the audiobook download
    (book_downloader.py) only, not the companion-PDF one — pdf_downloader.py
    keeps its existing hardcoded downloads_dir/Audible path unchanged, a
    deliberately narrower first cut rather than changing an already-working,
    unrelated download's behavior at the same time.
    """

    __tablename__ = "audible_download_destination"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
