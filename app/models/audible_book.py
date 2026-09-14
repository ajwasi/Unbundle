from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudibleBook(Base):
    """One owned Audible title, from GET /1.0/library. Same full-replace-on-
    refresh caching role SteamGame/GogGame play for their own libraries — see
    connectors/audible_connector.py for the real request shape.

    Most fields below (price/series/rating/benefit_id) are best-effort:
    Audible's API is undocumented and reverse-engineered, so their exact
    shape hasn't been confirmed against a real account yet — see
    audible_connector.py's own comments on each. Kept as plain nullable/
    defaulted columns specifically so a wrong guess is a cheap follow-up
    fix, not a migration.
    """

    __tablename__ = "audible_book"

    asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    runtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cover_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    purchase_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # List price at fetch time — not confirmed to be what was actually paid
    # (Audible's API has no confirmed "amount paid" field). See
    # audible_connector.py's fetch_library().
    price_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    series_title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    series_sequence: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    rating_average: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_finished: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    percent_complete: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    pdf_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # Raw field driving the owned/Plus-Catalog distinction — stored as-is
    # (not pre-interpreted) so audible_connector.is_owned() can be corrected
    # without needing a re-fetch. See that function's own comments.
    benefit_id: Mapped[str] = mapped_column(String(50), nullable=False, default="")
