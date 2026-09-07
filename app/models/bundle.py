from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Bundle(Base):
    """Cached summary of one Humble order/bundle. `raw_json` holds the full,
    verbatim `/api/v1/order/<gamekey>?all_tpkds=true` response so re-parsing
    never requires re-hitting Humble's API. `category`/`subproduct_count`/
    `key_count`/`purchased_at`/`amount_spent` are denormalized from raw_json
    at refresh time specifically so the bundle list can filter/sort/sum on
    them with plain SQL instead of json-parsing all 550+ rows on every page
    load.

    `category` is Humble's own `product.category` field — confirmed real
    values against a live 550-bundle account (2026-09-06): 'bundle' (a classic
    pay-what-you-want bundle), 'storefront' (an individual store purchase),
    'subscriptioncontent' (a Humble Choice month's games), 'subscriptionplan'
    (the Choice subscription billing record itself — legitimately has zero
    subproducts/entitlements, not a parsing gap), 'widget'.

    `purchased_at` is the order's `created` field, `amount_spent` is exactly
    that field name in the raw order (the actual amount charged — can differ
    slightly from the separate `total` field, which looks like the
    pay-what-you-want tier threshold rather than the exact charge; confirmed
    both fields present with zero missing/unparseable values across the full
    550-bundle library, so no fallback handling was needed for either).
    """

    __tablename__ = "bundle"

    gamekey: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    subproduct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    key_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    amount_spent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
