from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AmazonMusicTrack(Base):
    """One purchased track, as Amazon's web player reports it.

    Sourced from POST /api/showPurchasedTracks, which returns a server-driven
    UI payload rather than a data API — the fields below are what a rendered
    row actually carries, not what a music schema would ideally have. Notably
    absent, and therefore not columns: ISRC, purchase date, price, and any
    `purchased` flag. Those were all confirmed missing from the response
    (ISRC and a purchase timestamp do appear as query parameters on a signed
    download URL, but only after minting a download for that one track).
    "Purchased" is implied by the endpoint, not stored per row.

    `track_asin` is the primary key rather than `download_id` because it is the
    stable catalogue identity; download_id is a per-library-object UUID.
    """

    __tablename__ = "amazon_music_track"

    track_asin: Mapped[str] = mapped_column(String(20), primary_key=True)

    # The UUID keyed under the row's onCheckboxSelected.states — this is what
    # POST /api/downloadTrack takes as its `id`, and it appears again as
    # `cdoid` on the signed CloudFront URL that call returns.
    download_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    artist: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    artist_asin: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    album: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    album_asin: Mapped[str] = mapped_column(String(20), nullable=False, default="")

    # Kept as Amazon renders it ("3:57") alongside a parsed value, because the
    # display format is a UI string this app does not control — storing only
    # the parse would silently lose anything that doesn't match, and storing
    # only the string makes sorting by length impossible.
    duration_display: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Presigned S3 URL carrying X-Amz-Expires: it stops working some time after
    # the sync that fetched it. Refreshed on every sync; refreshed_at is what
    # lets the UI tell "stale link" apart from "no cover".
    cover_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cover_url_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Set when a track stops appearing in a full sync. Rows are flagged, never
    # deleted — a disappearance may be an Amazon-side glitch, and losing local
    # history to one bad sync is not recoverable.
    missing_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # True once every track sharing this album_asin resolves to one artist;
    # false marks a compilation, which is what routes its files to a single
    # "Various Artists" folder instead of scattering them. Computed at sync
    # time from the tracks themselves rather than costing a showCatalogAlbum
    # call per album.
    is_compilation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
