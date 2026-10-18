from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudibleAuthorProfile(Base):
    """Cached bio + photo for an author this account owns at least one book
    by, fetched from Audnexus (api.audnex.us) — a free, unauthenticated,
    community-run API that mirrors Audible's own author data, keyed by the
    same author asin Audible itself uses. Audible's own catalogue API never
    returns a bio or photo for an author, only {asin, name} per credit
    (confirmed against real catalog/products captures) — Audnexus is the
    only source for this that doesn't mean scraping Audible's consumer
    website directly.

    Refreshed wholesale alongside AudibleAuthorBook on every Audible sync
    (see audible_sync._fetch_author_profiles); an author Audnexus has never
    heard of, or a request that fails outright, just leaves no row here
    rather than failing the whole refresh — image_url/bio render blank in
    that case, same graceful-degradation posture the rest of this sync
    already applies to a single bad lookup.
    """

    __tablename__ = "audible_author_profile"

    author_asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    image_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    bio: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
