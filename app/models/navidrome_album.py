from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class NavidromeAlbum(Base):
    """One album from the user's Navidrome library, from getAlbumList2.
    Refreshed wholesale (full delete + insert) on each manual sync, same
    role AudiobookShelfItem/PlexAudiobookItem play for their own
    connectors — see sync/navidrome_sync.py. No stable id either app and
    Navidrome both carry, so matching against AmazonMusicTrack happens by
    normalized (album, artist), same approach plex_sync.py uses for titles.
    """

    __tablename__ = "navidrome_album"

    album_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    artist: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
