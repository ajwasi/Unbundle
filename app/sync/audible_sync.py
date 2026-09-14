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
from app.models.credential import SOURCE_AUDIBLE, STATUS_ERROR, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json

import audible


class NotConnectedError(Exception):
    """Raised when no Audible credential has been saved yet."""


def get_audible_credential(db: Session) -> dict | None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one_or_none()
    if cred is None or not cred.encrypted_payload:
        return None
    return decrypt_json(cred.encrypted_payload)


def save_authenticator(db: Session, auth: audible.Authenticator) -> None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one_or_none()
    if cred is None:
        cred = Credential(source=SOURCE_AUDIBLE)
        db.add(cred)
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
    cred = db.query(Credential).filter(Credential.source == SOURCE_AUDIBLE).one_or_none()
    if cred:
        cred.status = status
        cred.last_error = error
        db.commit()
