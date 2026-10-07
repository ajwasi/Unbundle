from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChirpSeriesBook(Base):
    """One book in a series the account has at least one book from. Every
    book on the series page is stored, owned or not; "missing" is computed
    against the owned library at read time, so it stays right even when the
    library changes without the series being refreshed.
    """

    __tablename__ = "chirp_series_book"

    series_url: Mapped[str] = mapped_column(String(300), primary_key=True)
    url_path: Mapped[str] = mapped_column(String(300), primary_key=True)
    series_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    authors: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    series_number: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    listing_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def series_slug(self) -> str:
        """series_url minus its "/series/" prefix, for building /chirp/series/{slug} links."""
        return self.series_url.removeprefix("/series/")

    @property
    def discount_pct(self) -> int | None:
        if self.current_price is None or not self.listing_price or self.listing_price <= 0:
            return None
        pct = round((1 - self.current_price / self.listing_price) * 100)
        return pct if pct > 0 else None
