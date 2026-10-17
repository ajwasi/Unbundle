from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudibleAuthorBook(Base):
    """One book by an author this account owns at least one title from, that
    this account does NOT yet own. Same "unowned only" contract as
    AudibleSeriesBook — owned books are never stored here, AudibleBook
    already has everything a display needs from one.

    Keyed by the author's name (not an asin) because that's what the
    library's own "authors" contributors list reliably gives us, and what
    the catalogue's author search takes. A book with several authors is
    stored once per owned author it matches, so each author's group on the
    page is complete on its own.
    """

    __tablename__ = "audible_author_book"

    author_name: Mapped[str] = mapped_column(String(300), primary_key=True)
    asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    authors: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    narrators: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    cover_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    series_title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    series_sequence: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    release_date: Mapped[str] = mapped_column(String(10), nullable=False, default="")
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
