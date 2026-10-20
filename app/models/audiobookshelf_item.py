from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudiobookShelfItem(Base):
    """One item in the user's configured AudiobookShelf library, from
    GET /api/libraries/{id}/items. Refreshed wholesale (full delete + insert)
    on each manual sync, same "cached snapshot" role SteamGame/GogGame play
    for their own libraries — see sync/audiobookshelf_sync.py for the match
    this gets used for (asin membership against AudibleBook, the only
    reliable key AudiobookShelf and Audible's catalog both carry).

    asin is nullable and NOT unique: AudiobookShelf has no notion of ASIN for
    its own non-Audible items, and the same ASIN could in principle appear
    twice (a duplicate import) without this app needing to care — matching
    only ever checks set membership, never relies on one row per asin.
    """

    __tablename__ = "audiobookshelf_item"

    library_item_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    asin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    author: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
