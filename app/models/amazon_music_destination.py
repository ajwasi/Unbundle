from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AmazonMusicDestination(Base):
    """A named directory purchased tracks can be downloaded into.

    Deliberately simpler than DownloadDestination (Humble's format/tag routing
    rules): a track has one file format (mp3, confirmed against a real signed
    delivery URL) and no tag system of its own, so there is nothing to route
    *by* — this is just a list of places the user has told the app it may
    write to, with one flagged as the default. `is_compilation` already
    decides the "Various Artists" vs. per-artist folder *within* whichever
    destination is used (see downloader.py); it does not pick the destination
    itself.
    """

    __tablename__ = "amazon_music_destination"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
