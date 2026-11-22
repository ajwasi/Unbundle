"""Credential encryption at rest. All reads/writes of `credential.encrypted_payload`
go through encrypt_json/decrypt_json — no connector or router touches Fernet directly.

Mirrors audiobook-tracker/app/security.py's shape exactly (same homelab convention
for this category of app): one shared APP_PASSWORD gate, a stateless signed session
cookie (no session table), Fernet-at-rest for stored third-party credentials.
"""

import base64
import functools
import hashlib
import hmac
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import settings
from app.models.credential import SOURCE_APP_AUTH, Credential

SESSION_COOKIE_NAME = "unbundle_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days
PBKDF2_ITERATIONS = 200_000

# Credentials are encrypted under a key derived from APP_SECRET_KEY with PBKDF2.
# Current ("v2") tokens carry their own random 16-byte salt inline:
#
#     v2:<urlsafe-base64 salt>:<fernet token>
#
# so every stored credential has a unique salt, nothing extra has to be
# persisted, and a DB backup stays self-contained and portable. The salt is not
# secret; it only defeats precomputed key tables shared across installations.
#
# Tokens written before that ("v1") used a fixed application-level salt, baked
# into the derived key of every credential encrypted that way. It is kept here
# solely so decrypt_json() can still read them; it is deliberately NOT renamed
# to "unbundle" with the rest of the branding, because changing it would make
# those rows permanently undecryptable. Any re-save of a credential (or
# `python -m app.cli rotate-secret-key`) rewrites it as v2.
_LEGACY_FIXED_SALT = b"humble-tracker:credential-encryption:v1"
_V2_PREFIX = b"v2:"
_SALT_BYTES = 16
CREDENTIAL_KDF_ITERATIONS = 200_000


@functools.lru_cache(maxsize=64)
def _kdf(secret: str, salt: bytes) -> bytes:
    # 200k PBKDF2 rounds is deliberately slow (~100ms+) — the point is making
    # offline brute-force of a weak secret expensive. It is only worth paying
    # once per distinct (secret, salt) per process, and AuthMiddleware reads the
    # app-auth credential on every request, so the result is cached: a pure
    # function of its inputs, safe to memoise. The cache holds one entry per
    # stored credential salt plus rotation temporaries.
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, CREDENTIAL_KDF_ITERATIONS)
    return base64.urlsafe_b64encode(digest)


def _derive_key(secret: str) -> bytes:
    """The v1 (fixed-salt) key — read-only compatibility, never used to encrypt."""
    return _kdf(secret, _LEGACY_FIXED_SALT)


@functools.lru_cache(maxsize=8)
def _derive_key_legacy(secret: str) -> bytes:
    """Original KDF (single unsalted, un-iterated SHA-256) — kept only so
    decrypt_json() can still open credentials written before the PBKDF2
    upgrade above. Never used for new encryption; see `python -m app.cli
    rotate-secret-key` for migrating old rows onto the new KDF (and onto a
    new key at the same time, if desired) instead of relying on this
    fallback indefinitely.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_json(data: dict, secret_key: str | None = None) -> bytes:
    """secret_key overrides settings.app_secret_key — used only by the
    rotate-secret-key CLI tool to encrypt under a *new* key before that key
    is actually the one configured in the running environment.
    """
    payload = json.dumps(data).encode("utf-8")
    salt = os.urandom(_SALT_BYTES)
    key = _kdf(secret_key if secret_key is not None else settings.app_secret_key, salt)
    return _V2_PREFIX + base64.urlsafe_b64encode(salt) + b":" + Fernet(key).encrypt(payload)


def decrypt_json(token: bytes, secret_key: str | None = None) -> dict:
    secret = secret_key if secret_key is not None else settings.app_secret_key
    try:
        if token.startswith(_V2_PREFIX):
            _, salt_b64, fernet_token = token.split(b":", 2)
            payload = Fernet(_kdf(secret, base64.urlsafe_b64decode(salt_b64))).decrypt(fernet_token)
        else:
            try:
                payload = Fernet(_derive_key(secret)).decrypt(token)
            except InvalidToken:
                payload = Fernet(_derive_key_legacy(secret)).decrypt(token)
    except (InvalidToken, ValueError) as exc:
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


def _stored_password_hash(db: Session) -> str | None:
    payload = Credential.get_payload(db, SOURCE_APP_AUTH)
    return payload.get("password_hash") if payload else None


def has_db_password(db: Session) -> bool:
    """True if a password has been set via `python -m app.cli set-password` —
    a real, independently-sufficient login method, not just an APP_PASSWORD
    fallback. app/oidc.py's is_auth_configured() needs this check too: without
    it, a DB-only password (no APP_PASSWORD env var set at all) never actually
    enables AuthMiddleware's gate, leaving the app silently wide open despite
    a password having been "set".
    """
    return _stored_password_hash(db) is not None


def check_app_password(candidate: str, db: Session) -> bool:
    """A DB-stored password (set via `python -m app.cli set-password`, see cli.py) always
    takes precedence over APP_PASSWORD when present — this is what lets the emergency reset
    tool change the effective password at runtime without touching env vars or restarting.
    Falls back to the env-var plaintext comparison otherwise (unchanged original behavior).
    """
    stored_hash = _stored_password_hash(db)
    if stored_hash:
        return verify_password_hash(candidate, stored_hash)
    return bool(settings.app_password) and hmac.compare_digest(candidate, settings.app_password)


def create_session_token(identity: dict | None = None) -> str:
    payload = {"authenticated": True}
    if identity:
        payload["identity"] = identity
    return _serializer().dumps(payload)


def decode_session_token(token: str | None) -> dict | None:
    """The full signed payload (possibly with an "identity" key — see
    oidc.py's identity_from_userinfo) rather than just a yes/no, so
    AuthMiddleware can resolve request.state.identity_label from it directly
    instead of decoding the cookie twice."""
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature:
        return None


def verify_session_token(token: str | None) -> bool:
    return decode_session_token(token) is not None
