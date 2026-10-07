from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudibleSeriesBook(Base):
    """One book from a series this account owns at least one title from,
    that this account does NOT yet own. Unlike Chirp's equivalent table,
    owned siblings are never stored here — re-fetching full catalogue
    details for a book already in AudibleBook would be a wasted API call,
    and that table already has everything a display would need from one.
    """

    __tablename__ = "audible_series_book"

    series_asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    series_title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    authors: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    narrators: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    cover_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    sequence: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def discount_pct(self) -> int | None:
        if self.current_price is None or not self.list_price or self.list_price <= 0:
            return None
        pct = round((1 - self.current_price / self.list_price) * 100)
        return pct if pct > 0 else None
