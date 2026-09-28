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

    `download_id` is the primary key, not `track_asin`. An earlier version
    keyed on track_asin, assuming it was the stable catalogue identity; a
    live sync disproved that — 10,000 purchased rows produced only 27
    distinct track_asin values (each one silently overwritten by whichever
    row synced last) while download_id was distinct on every single row.
    Whatever track_asin actually keys off — the album is the leading guess,
    never confirmed — it is kept only as metadata below.
    """

    __tablename__ = "amazon_music_track"

    # The UUID keyed under the row's onCheckboxSelected.states — this is what
    # POST /api/downloadTrack takes as its `id`, and it appears again as
    # `cdoid` on the signed CloudFront URL that call returns.
    download_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    track_asin: Mapped[str] = mapped_column(String(20), nullable=False, default="", index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    artist: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    artist_asin: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    album: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    album_asin: Mapped[str] = mapped_column(String(20), nullable=False, default="")

    # 1-based position within its album's own tracklist — confirmed available
    # only via an on-demand fetch of Amazon's catalog-browsing page for the
    # album (see amazon_music_sync.sync_album_track_order), not from the
    # purchased-library sync this app otherwise relies on. There is no
    # explicit "track number" field even there; this is the row's position in
    # that response's own ordered track list, matched back to this row by
    # title. Null until that on-demand fetch has run and found a match.
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

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

    # Neither is available from the purchased-library sync — showPurchasedTracks
    # carries neither field at all. Both ride along in the signed delivery URL
    # /api/downloadTrack returns (see amazon_music_connector.parse_delivery_url_
    # metadata), so both stay blank until a track has actually been downloaded
    # at least once; downloading is the only thing that ever populates them.
    isrc: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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
