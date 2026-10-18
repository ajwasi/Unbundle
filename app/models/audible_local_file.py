from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SOURCE_DOWNLOAD = "download"
SOURCE_SCAN = "scan"


class AudibleLocalFile(Base):
    """Where a book's audiobook file is known to sit on disk — written when a
    download completes and when a folder scan finds a file that predates this
    app (or arrived outside it), so either way the app never re-downloads
    something it already has.

    Deliberately no foreign key to audible_book: a library refresh replaces
    every AudibleBook row, and this record has to outlive that — it's the
    app's own memory of what's on disk, not a property of the catalogue entry.
    """

    __tablename__ = "audible_local_file"

    asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_DOWNLOAD)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
