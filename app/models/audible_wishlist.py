from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudibleWishlistItem(Base):
    """One title on the Audible wishlist, from GET /1.0/wishlist.

    Unlike the catalogue endpoint, wishlist responses sort by price server
    side and a wishlist is tens of items, so a full refresh is one or two
    calls rather than the sixty a catalogue sweep needs to find the same
    discounts.

    `current_price`/`list_price` are nullable because the price response group
    is not guaranteed to populate both — an item with no list price still
    belongs here, it just can't show a discount percentage.
    """

    __tablename__ = "audible_wishlist_item"

    asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    subtitle: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    authors: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    narrators: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    cover_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    runtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")

    # Amazon's own "added to wishlist" date when it supplies one — used for
    # the default sort so a freshly added title is easy to find again.
    added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Set when an item stops appearing — bought, or removed on Amazon's side.
    # Flagged rather than deleted so its price history survives.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def discount_pct(self) -> int | None:
        """None unless both prices are known and the list price is the higher
        of the two — a "discount" computed from one price is meaningless, and
        a negative one means the data disagrees with itself."""
        if self.current_price is None or not self.list_price or self.list_price <= 0:
            return None
        pct = round((1 - self.current_price / self.list_price) * 100)
        return pct if pct > 0 else None


class AudibleWishlistPrice(Base):
    """A price observation for a wishlisted title.

    Rows are only written when the price actually moved, so the table stays
    small and every row means something happened. This is what turns "it is
    $6.99" into "it has never been cheaper", which is the part that tells you
    whether to buy now.
    """

    __tablename__ = "audible_wishlist_price"

    id: Mapped[int] = mapped_column(primary_key=True)
    asin: Mapped[str] = mapped_column(ForeignKey("audible_wishlist_item.asin"), nullable=False, index=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
