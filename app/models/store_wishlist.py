"""Steam and GOG wishlist rows.

Parallel tables rather than columns added to audible_wishlist_item: that one
is already shipped and holding real data, and the three sources genuinely
differ (Steam is keyed on an integer appid and needs a cached detail fetch,
GOG is keyed on a product id and gets everything in two calls). The Wishlist
page merges them; nothing here has to know about the others.

Both mirror the Audible pattern deliberately, so the shared sync helpers in
app/sync/wishlist_common.py can drive all three without branching on source.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class _WishlistPriceMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class SteamWishlistItem(Base):
    """One wishlisted Steam app.

    IWishlistService/GetWishlist returns appids and nothing else, so name,
    art and price come from a second lookup per app — store/api/appdetails
    rejects batched appids with a 400, confirmed. `details_fetched_at` is what
    keeps that affordable: names and art never change, so only an appid never
    seen before pays the full fetch, and later refreshes ask only for price.
    """

    __tablename__ = "steam_wishlist_item"

    appid: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    header_image: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    developers: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    short_description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Steam's own wishlist ordering, lowest first.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    # Steam reports the percentage directly, so it is stored rather than
    # derived — the number shown is the one Steam advertises.
    discount_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    details_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SteamWishlistPrice(_WishlistPriceMixin, Base):
    __tablename__ = "steam_wishlist_price"
    appid: Mapped[int] = mapped_column(ForeignKey("steam_wishlist_item.appid"), nullable=False, index=True)


class GogWishlistItem(Base):
    """One wishlisted GOG product.

    Cheaper than Steam: the wishlist itself is one authenticated call, and
    api.gog.com/products/prices prices the whole list in a single batch
    regardless of size.
    """

    __tablename__ = "gog_wishlist_item"

    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    cover_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    store_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")

    details_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def discount_pct(self) -> int | None:
        """Derived, unlike Steam's — GOG's price endpoint reports amounts
        only. None unless both are known and the list price is genuinely
        higher."""
        if self.current_price is None or not self.list_price or self.list_price <= 0:
            return None
        pct = round((1 - self.current_price / self.list_price) * 100)
        return pct if pct > 0 else None


class GogWishlistPrice(_WishlistPriceMixin, Base):
    __tablename__ = "gog_wishlist_price"
    product_id: Mapped[int] = mapped_column(ForeignKey("gog_wishlist_item.product_id"), nullable=False, index=True)
