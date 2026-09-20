"""Refreshes the cached Audible library — same shape as sync/steam_sync.py
and sync/gog_sync.py, minus the entitlement-matching step neither of those
skip: Audible purchases never come through a Humble bundle, so there's
nothing here to cross-reference against BundleEntitlement.

The full Authenticator dict (not just a refresh token) is persisted and
reloaded on every refresh — Authenticator.refresh_access_token() mutates the
authenticator in place when its access_token has expired (confirmed reading
audible/auth.py's own _apply_bearer_auth_flow), so the freshly-used
authenticator is always re-saved afterward, the same "tokens may rotate,
persist whatever came back" philosophy gog_sync.py already applies to its
own refresh_token.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.connectors import audible_connector
from app.models.audible_book import AudibleBook
from app.models.audible_wishlist import AudibleWishlistItem, AudibleWishlistPrice
from app.models.credential import SOURCE_AUDIBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import encrypt_json

import audible


class NotConnectedError(Exception):
    """Raised when no Audible credential has been saved yet."""


def get_audible_credential(db: Session) -> dict | None:
    return Credential.get_payload(db, SOURCE_AUDIBLE)


def save_authenticator(db: Session, auth: audible.Authenticator) -> None:
    cred = Credential.get_or_create(db, SOURCE_AUDIBLE)
    cred.encrypted_payload = encrypt_json(auth.to_dict())
    db.commit()


async def refresh_audible_library(db: Session) -> int:
    """Returns the number of titles fetched. Raises NotConnectedError —
    caller surfaces the message."""
    payload = get_audible_credential(db)
    if not payload:
        raise NotConnectedError("Audible is not connected yet — connect it in Settings.")

    try:
        # from_dict() pops "locale_code" off the dict it's given (confirmed
        # in audible/auth.py) — pass a copy so a decrypt-then-mutate-then-
        # re-encrypt round trip never silently drops the stored locale.
        auth = audible.Authenticator.from_dict(dict(payload))
        books = await audible_connector.fetch_library(auth)
        save_authenticator(db, auth)
    except Exception as exc:
        _set_credential_status(db, STATUS_ERROR, str(exc))
        raise

    db.query(AudibleBook).delete()
    now = datetime.utcnow()
    for b in books:
        db.add(
            AudibleBook(
                asin=b.asin,
                title=b.title,
                author=b.author,
                narrator=b.narrator,
                runtime_minutes=b.runtime_minutes,
                cover_url=b.cover_url,
                fetched_at=now,
                purchase_date=b.purchase_date,
                price_amount=b.price_amount,
                price_currency=b.price_currency,
                series_title=b.series_title,
                series_sequence=b.series_sequence,
                rating_average=b.rating_average,
                description=b.description,
                is_finished=b.is_finished,
                percent_complete=b.percent_complete,
                pdf_url=b.pdf_url,
                benefit_id=b.benefit_id,
            )
        )
    db.commit()

    _set_credential_status(db, STATUS_OK, None)
    return len(books)


def _set_credential_status(db: Session, status: str, error: str | None) -> None:
    Credential.set_status(db, SOURCE_AUDIBLE, status, error)


def _record_price_if_changed(db: Session, item: AudibleWishlistItem, now: datetime) -> bool:
    """Append a price observation only when it actually moved.

    Every row in audible_wishlist_price therefore means something happened,
    which is what makes "cheapest it has been" answerable without storing a
    row per title per refresh forever.
    """
    latest = (
        db.query(AudibleWishlistPrice)
        .filter(AudibleWishlistPrice.asin == item.asin)
        .order_by(AudibleWishlistPrice.captured_at.desc())
        .first()
    )
    if latest and latest.price == item.current_price and latest.list_price == item.list_price:
        return False
    db.add(
        AudibleWishlistPrice(
            asin=item.asin,
            price=item.current_price,
            list_price=item.list_price,
            currency=item.currency,
            captured_at=now,
        )
    )
    return True


async def refresh_audible_wishlist(db: Session) -> dict:
    """Pull the wishlist and upsert it. Returns a summary for the UI."""
    payload = get_audible_credential(db)
    if not payload:
        raise NotConnectedError("Audible is not connected yet — connect it in Settings.")

    try:
        auth = audible.Authenticator.from_dict(dict(payload))
        entries = await audible_connector.fetch_wishlist(auth)
        save_authenticator(db, auth)
    except Exception as exc:
        _set_credential_status(db, STATUS_ERROR, str(exc))
        raise

    now = datetime.utcnow()
    seen: set[str] = set()
    new = price_changes = 0

    for entry in entries:
        seen.add(entry.asin)
        row = db.get(AudibleWishlistItem, entry.asin)
        if row is None:
            row = AudibleWishlistItem(asin=entry.asin, first_seen_at=now)
            db.add(row)
            new += 1
        row.title = entry.title
        row.subtitle = entry.subtitle
        row.authors = entry.authors
        row.narrators = entry.narrators
        row.cover_url = entry.cover_url
        row.runtime_minutes = entry.runtime_minutes
        row.current_price = entry.current_price
        row.list_price = entry.list_price
        row.currency = entry.currency
        row.added_at = entry.added_at
        row.last_seen_at = now
        row.removed_at = None
        db.flush()  # the price row's FK needs this item to exist first
        if _record_price_if_changed(db, row, now):
            price_changes += 1

    removed = 0
    if seen:
        stale = (
            db.query(AudibleWishlistItem)
            .filter(AudibleWishlistItem.removed_at.is_(None))
            .filter(~AudibleWishlistItem.asin.in_(seen))
            .all()
        )
        for row in stale:
            row.removed_at = now
            removed += 1

    db.commit()
    _set_credential_status(db, STATUS_OK, None)
    return {"total": len(entries), "new": new, "price_changes": price_changes, "removed": removed}
