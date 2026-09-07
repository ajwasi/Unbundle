"""Credential encryption at rest. All reads/writes of `credential.encrypted_payload`
go through encrypt_json/decrypt_json — no connector or router touches Fernet directly.

Mirrors audiobook-tracker/app/security.py's shape exactly (same homelab convention
for this category of app): one shared APP_PASSWORD gate, a stateless signed session
cookie (no session table), Fernet-at-rest for stored third-party credentials.
"""

import base64
import hashlib
import hmac
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import settings
from app.models.credential import SOURCE_APP_AUTH, Credential

SESSION_COOKIE_NAME = "humble_tracker_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days
PBKDF2_ITERATIONS = 200_000


def _derive_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_derive_key(settings.app_secret_key))


def encrypt_json(data: dict) -> bytes:
    payload = json.dumps(data).encode("utf-8")
    return _fernet().encrypt(payload)


def decrypt_json(token: bytes) -> dict:
    try:
        payload = _fernet().decrypt(token)
    except InvalidToken as exc:
        raise ValueError("Could not decrypt stored credential — APP_SECRET_KEY may have changed") from exc
    return json.loads(payload.decode("utf-8"))


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret_key, salt="session")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password_hash(candidate: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def check_app_password(candidate: str, db: Session) -> bool:
    """A DB-stored password (set via `python -m app.cli set-password`, see cli.py) always
    takes precedence over APP_PASSWORD when present — this is what lets the emergency reset
    tool change the effective password at runtime without touching env vars or restarting.
    Falls back to the env-var plaintext comparison otherwise (unchanged original behavior).
    """
    cred = db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one_or_none()
    if cred and cred.encrypted_payload:
        stored_hash = decrypt_json(cred.encrypted_payload).get("password_hash")
        if stored_hash:
            return verify_password_hash(candidate, stored_hash)
    return bool(settings.app_password) and hmac.compare_digest(candidate, settings.app_password)


def create_session_token() -> str:
    return _serializer().dumps({"authenticated": True})


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature:
        return False
    return True
