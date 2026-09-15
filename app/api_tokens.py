"""Bearer tokens for programmatic (non-browser) access to this app — every
route a session cookie can reach, a valid token can reach too (see
deps.py's AuthMiddleware), since this is a single-user app with no
permission/role system to scope a token down against.

Deliberately a fast SHA-256 rather than security.py's slow PBKDF2 hashing:
the token itself already carries 256 bits of secrets.token_urlsafe entropy,
so KDF stretching (which defends against brute-forcing a weak, human-chosen
secret) protects against a threat that doesn't apply here. Verifying is a
hash-and-look-up-by-unique-index, not a direct secret compare, so it isn't
the kind of timing attack hmac.compare_digest exists for either.
"""

import hashlib
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.api_token import ApiToken

TOKEN_PREFIX = "unbundle_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(db: Session, name: str) -> tuple[ApiToken, str]:
    """Returns the row and the one-and-only plaintext — this is the sole
    moment it's ever available; the caller must show it to the user now."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(name=name, token_hash=_hash(raw))
    db.add(row)
    db.commit()
    return row, raw


def verify_token(db: Session, raw_token: str) -> ApiToken | None:
    if not raw_token.startswith(TOKEN_PREFIX):
        return None
    row = db.query(ApiToken).filter(ApiToken.token_hash == _hash(raw_token)).one_or_none()
    if row is not None:
        row.last_used_at = datetime.utcnow()
        db.commit()
    return row


def delete_token(db: Session, token_id: int) -> None:
    db.query(ApiToken).filter(ApiToken.id == token_id).delete()
    db.commit()
