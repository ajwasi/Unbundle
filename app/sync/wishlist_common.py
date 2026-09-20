"""Shared wishlist sync behaviour.

The three wishlist sources keep parallel tables (see models/store_wishlist.py
for why), but the *rules* are identical everywhere, and duplicating them three
times is how they drift apart:

  * a price row is written only when the price actually moved, so every row
    means something happened;
  * an item that stops appearing is flagged, never deleted, so its price
    history survives a bad refresh or a title bought elsewhere.

Parameterised by model rather than branching on source, so adding a fourth
store needs no changes here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session


def record_price_if_changed(
    db: Session,
    price_model: Any,
    owner_field: str,
    owner_value: Any,
    price: float | None,
    list_price: float | None,
    currency: str,
    now: datetime,
) -> bool:
    """True when a new observation was written."""
    owner_column = getattr(price_model, owner_field)
    latest = (
        db.query(price_model)
        .filter(owner_column == owner_value)
        .order_by(price_model.captured_at.desc())
        .first()
    )
    if latest and latest.price == price and latest.list_price == list_price:
        return False
    db.add(
        price_model(
            **{owner_field: owner_value},
            price=price,
            list_price=list_price,
            currency=currency,
            captured_at=now,
        )
    )
    return True


def flag_removed(db: Session, item_model: Any, key_field: str, seen: set, now: datetime) -> int:
    """Flag rows that did not appear in this refresh. Returns how many.

    An empty `seen` is treated as "nothing to compare against" rather than
    "everything is gone": a source returning nothing is far more likely to be
    a transient failure than a wishlist genuinely emptied, and acting on that
    would flag the entire table.
    """
    if not seen:
        return 0
    key_column = getattr(item_model, key_field)
    stale = (
        db.query(item_model)
        .filter(item_model.removed_at.is_(None))
        .filter(~key_column.in_(seen))
        .all()
    )
    for row in stale:
        row.removed_at = now
    return len(stale)
