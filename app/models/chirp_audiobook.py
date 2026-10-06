from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChirpAudiobook(Base):
    """One owned Chirp audiobook, from currentUserAudiobooks. Refreshed
    wholesale (delete-and-reinsert every row) on each manual sync, same
    "cached snapshot" role SteamGame plays for Steam's own library — see
    chirp_sync.py for why this has to walk every page itself first (Chirp's
    own API caps each response at ~20 items, confirmed live).

    listing_price/discount_price are Chirp's current storefront price for
    the book, not what this account actually paid — Chirp's confirmed API
    has no purchase-price or purchase-date field anywhere (see
    chirp_connector.py's own module docstring); showing today's price as
    "Price" is honest, showing it as what was paid would not be.
    """

    __tablename__ = "chirp_audiobook"

    purchase_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    audiobook_id: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    authors: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    narrators: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    url_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    cover_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    progress_status: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    position_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    playable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    series_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    series_number: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    listing_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Denormalized from the order-history page at sync time — the earliest
    # purchase of this book, and what was actually paid for it (0.0 for a
    # free title). Same denormalize-at-sync shape Bundle.purchased_at uses.
    purchased_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def display_price(self) -> float | None:
        """The price to actually show: Chirp's discounted price when one's
        on offer, otherwise the plain listing price — same "discount wins"
        convention as every other store's price column in this app."""
        if self.discount_price is not None:
            return self.discount_price
        return self.listing_price
