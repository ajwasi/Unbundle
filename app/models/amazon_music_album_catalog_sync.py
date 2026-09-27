from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AmazonMusicAlbumCatalogSync(Base):
    """Records that an album's real track order was fetched on demand from
    Amazon's catalog-browsing page (POST /api/showHome with a deeplink of
    /albums/<asin>) — separate from AmazonMusicTrack.track_number so "has
    this album been synced at all" survives even for an album where none of
    its tracks happened to match (tracks_matched can be 0 while this row
    still exists), and so the UI can show *when* and *how completely* rather
    than just a boolean.

    Deliberately not folded into the regular purchased-library sync: that
    sync already makes two full passes over the whole library, and hitting
    this per-album catalog endpoint for every album on top of that would be
    one more request per album for data most of the time nobody looks at.
    Fetched only when a user opens that specific album's page and asks for it.
    """

    __tablename__ = "amazon_music_album_catalog_sync"

    album_asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    tracks_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tracks_matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
