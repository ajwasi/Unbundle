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

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.connectors import audible_connector
from app.models.audible_author_book import AudibleAuthorBook
from app.models.audible_author_profile import AudibleAuthorProfile
from app.models.audible_book import AudibleBook
from app.models.audible_book_download import AudibleBookDownload
from app.models.audible_pdf_download import AudiblePdfDownload
from app.models.audible_series_book import AudibleSeriesBook
from app.models.audible_wishlist import AudibleWishlistItem, AudibleWishlistPrice
from app.models.credential import SOURCE_AUDIBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import encrypt_json

import audible

logger = logging.getLogger(__name__)


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

    # An upsert, not the delete-everything-then-reinsert this used to be:
    # AudibleBookDownload and AudiblePdfDownload both carry a foreign key to
    # audible_book.asin, and on Postgres (unlike this app's own SQLite,
    # which never turns on PRAGMA foreign_keys) that FK is enforced
    # immediately — deleting every book row, even ones still owned, failed
    # outright the moment any book had a completed download. Updating
    # still-owned rows in place means that delete never happens for the
    # common case (the same books, refreshed); only a book that's actually
    # left the account gets its row deleted at all.
    existing_books = {b.asin: b for b in db.query(AudibleBook).all()}
    now = datetime.utcnow()
    for b in books:
        row = existing_books.get(b.asin)
        if row is None:
            row = AudibleBook(asin=b.asin)
            db.add(row)
        row.title = b.title
        row.author = b.author
        row.narrator = b.narrator
        row.runtime_minutes = b.runtime_minutes
        row.cover_url = b.cover_url
        row.fetched_at = now
        row.purchase_date = b.purchase_date
        row.price_amount = b.price_amount
        row.price_currency = b.price_currency
        row.series_title = b.series_title
        row.series_sequence = b.series_sequence
        row.series_asin = b.series_asin
        row.rating_average = b.rating_average
        row.description = b.description
        row.is_finished = b.is_finished
        row.percent_complete = b.percent_complete
        row.pdf_url = b.pdf_url
        row.benefit_id = b.benefit_id

    removed_asins = set(existing_books) - {b.asin for b in books}
    if removed_asins:
        # A book that's actually gone (returned, etc.) may still have
        # download-job history referencing it — deleted explicitly here
        # rather than relied on the database's own FK cascade (which
        # Postgres would need ondelete="CASCADE" for, and SQLite wouldn't
        # enforce at all), so this stays correct on both backends.
        db.query(AudibleBookDownload).filter(AudibleBookDownload.asin.in_(removed_asins)).delete(synchronize_session=False)
        db.query(AudiblePdfDownload).filter(AudiblePdfDownload.asin.in_(removed_asins)).delete(synchronize_session=False)
        db.query(AudibleBook).filter(AudibleBook.asin.in_(removed_asins)).delete(synchronize_session=False)
    db.commit()

    try:
        series_rows = await _fetch_series_rows(auth, books)
    except Exception as exc:
        logger.warning("audible: series refresh failed, keeping previous series data: %s", exc)
        series_rows = None
    if series_rows is not None:
        db.query(AudibleSeriesBook).delete()
        for row in series_rows:
            db.add(row)
        db.commit()

    try:
        author_rows = await _fetch_author_rows(auth, books)
    except Exception as exc:
        logger.warning("audible: author refresh failed, keeping previous author data: %s", exc)
        author_rows = None
    if author_rows is not None:
        db.query(AudibleAuthorBook).delete()
        for row in author_rows:
            db.add(row)
        db.commit()

        try:
            profile_rows = await _fetch_author_profiles(author_rows)
        except Exception as exc:
            logger.warning("audible: author profile refresh failed, keeping previous profile data: %s", exc)
            profile_rows = None
        if profile_rows is not None:
            db.query(AudibleAuthorProfile).delete()
            for row in profile_rows:
                db.add(row)
            db.commit()

    _set_credential_status(db, STATUS_OK, None)
    return len(books)


async def _fetch_series_rows(
    auth: audible.Authenticator, books: list[audible_connector.AudibleBookData]
) -> list[AudibleSeriesBook]:
    """One series-relationships call per distinct owned series, then one
    catalogue call per sibling not already owned — owned siblings are never
    re-fetched, since AudibleBook already has everything a display needs
    from one. A series or a sibling that fails to load is skipped rather
    than failing the whole refresh.
    """
    owned_asins = {b.asin for b in books}
    representative: dict[str, str] = {}
    for b in books:
        if b.series_asin and b.series_asin not in representative:
            representative[b.series_asin] = b.series_title

    rows: list[AudibleSeriesBook] = []
    now = datetime.utcnow()
    for series_asin, series_title in representative.items():
        try:
            children = await audible_connector.fetch_series_children(auth, series_asin)
        except Exception as exc:
            logger.warning("audible: series %s unavailable, skipping: %s", series_asin, exc)
            continue
        for asin, sequence in children:
            if asin in owned_asins:
                continue
            try:
                data = await audible_connector.probe_catalog_product(auth, asin)
            except Exception as exc:
                logger.warning("audible: series book %s unavailable, skipping: %s", asin, exc)
                continue
            parsed = audible_connector.parse_series_book_item(data.get("product") or {}, sequence)
            if parsed is None:
                continue
            rows.append(
                AudibleSeriesBook(
                    series_asin=series_asin,
                    asin=parsed.asin,
                    series_title=series_title,
                    title=parsed.title,
                    authors=parsed.authors,
                    narrators=parsed.narrators,
                    cover_url=parsed.cover_url,
                    sequence=parsed.sequence,
                    current_price=parsed.current_price,
                    list_price=parsed.list_price,
                    currency=parsed.currency,
                    fetched_at=now,
                )
            )
    return rows


def _is_contributor_credit(name: str) -> bool:
    """True for a non-author credit Audible's own `authors` array still
    carries, with the role appended straight into the name string — e.g.
    "Richard Howard - translator" — rather than a separate field. Confirmed
    from a real catalog/products response (2026-10-18) captured while
    debugging the Missing by Author tab coming back empty: searching
    Audible's catalogue by a name like that only ever turns up the small
    handful of editions sharing that exact translator credit, which are
    usually the very book this app already owns — so every such search
    came back with nothing new, not because nothing was missing but because
    the "author" being searched for was never a real author to begin with.
    A real name is never going to contain " - " followed by more text this
    way (hyphenated names don't have spaces around the hyphen), so this is
    a safe, non-guessing way to tell the two apart using Audible's own
    consistent formatting rather than a hardcoded role word-list.
    """
    return " - " in name


async def _fetch_author_rows(
    auth: audible.Authenticator, books: list[audible_connector.AudibleBookData]
) -> list[AudibleAuthorBook]:
    """One catalogue search per distinct owned author (matched
    case-insensitively); anything already owned is dropped, and a
    non-author contributor credit (translator, foreword, etc. — see
    _is_contributor_credit) is never searched for in the first place. An
    author whose lookup fails is skipped rather than failing the whole
    refresh.
    """
    owned_asins = {b.asin for b in books}
    authors: dict[str, str] = {}
    for b in books:
        for name in b.author_names:
            if _is_contributor_credit(name):
                continue
            authors.setdefault(name.casefold(), name)

    rows: list[AudibleAuthorBook] = []
    now = datetime.utcnow()
    for author_name in authors.values():
        try:
            found = await audible_connector.fetch_author_books(auth, author_name)
        except Exception as exc:
            logger.warning("audible: author %s unavailable, skipping: %s", author_name, exc)
            continue
        for parsed in found:
            if parsed.asin in owned_asins:
                continue
            rows.append(
                AudibleAuthorBook(
                    author_name=author_name,
                    asin=parsed.asin,
                    author_asin=parsed.author_asin,
                    title=parsed.title,
                    authors=parsed.authors,
                    narrators=parsed.narrators,
                    cover_url=parsed.cover_url,
                    series_title=parsed.series_title,
                    series_sequence=parsed.series_sequence,
                    release_date=parsed.release_date,
                    current_price=parsed.current_price,
                    list_price=parsed.list_price,
                    currency=parsed.currency,
                    fetched_at=now,
                )
            )
    return rows


async def _fetch_author_profiles(author_rows: list[AudibleAuthorBook]) -> list[AudibleAuthorProfile]:
    """One Audnexus lookup per distinct author appearing in author_rows (see
    AudibleAuthorProfile's own docstring for why Audnexus and not Audible
    itself). A profile that fails to load, or an author row with no captured
    asin at all, is just skipped — same "never fail the whole refresh over
    one bad lookup" posture as every other per-item fetch in this module.
    """
    author_asins = {row.author_asin for row in author_rows if row.author_asin}
    rows: list[AudibleAuthorProfile] = []
    now = datetime.utcnow()
    for author_asin in author_asins:
        try:
            profile = await audible_connector.fetch_author_profile(author_asin)
        except Exception as exc:
            logger.warning("audible: author profile %s unavailable, skipping: %s", author_asin, exc)
            continue
        if profile is None:
            continue
        rows.append(
            AudibleAuthorProfile(
                author_asin=profile.author_asin,
                name=profile.name,
                image_url=profile.image_url,
                bio=profile.bio,
                fetched_at=now,
            )
        )
    return rows


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
        row.rating = entry.rating
        row.rating_count = entry.rating_count
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


