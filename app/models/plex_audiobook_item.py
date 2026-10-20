from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PlexAudiobookItem(Base):
    """One album-level item (a book) from the user's configured Plex
    audiobook library, from GET /library/sections/{key}/all?type=9 (album).
    Refreshed wholesale (full delete + insert) on each manual sync, same
    "cached snapshot" role AudiobookShelfItem plays — see
    sync/plex_sync.py.

    Unlike AudiobookShelf, Plex has no ASIN field to match on, so matching
    against AudibleBook happens by normalized title (app.audible.scan's own
    normalize(), already used there for the same "match a title string
    loosely" job) — title is the only field actually stored here that
    matters for matching; author is kept for display/debugging only.
    """

    __tablename__ = "plex_audiobook_item"

    rating_key: Mapped[str] = mapped_column(String(50), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    author: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
