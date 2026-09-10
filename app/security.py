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

SESSION_COOKIE_NAME = "humble_tracker_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days
PBKDF2_ITERATIONS = 200_000

# Fixed, non-secret, application-level salt — not per-installation-random. The
# risk this KDF defends against is an offline brute-force of a possibly weak,
# hand-typed APP_SECRET_KEY (there's exactly one installation's worth of
# credentials to protect, so a static salt costs nothing here); what actually
# slows that attack down is the iteration count, which a per-call random salt
# wouldn't add on its own anyway — and a *random* salt would need its own
# persisted storage with no natural home (Fernet's key must be reproducible
# from settings.app_secret_key alone on every call, with no other state).
_CREDENTIAL_KEY_SALT = b"humble-tracker:credential-encryption:v1"
CREDENTIAL_KDF_ITERATIONS = 200_000


@functools.lru_cache(maxsize=8)
def _derive_key(secret: str) -> bytes:
    # 200k PBKDF2 rounds is deliberately slow (~100ms+) — the whole point is
    # making offline brute-force of a weak secret expensive. That cost is only
    # worth paying once per distinct secret per process, though: settings.app_secret_key
    # is fixed for the life of a running process (rotating it requires the CLI tool
    # below plus a restart), and AuthMiddleware calls into this, via is_auth_configured(),
    # on every single request — uncached, that would add 100ms+ to every page load
    # in the app, not just credential reads. Caching is safe here specifically because
    # this is a pure function of the secret string, not because slow-KDF-on-first-use
    # matters any less; it still fully applies to an offline attacker with just the DB file.
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), _CREDENTIAL_KEY_SALT, CREDENTIAL_KDF_ITERATIONS)
    return base64.urlsafe_b64encode(digest)


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


def _fernet_for_key(secret: str) -> Fernet:
    return Fernet(_derive_key(secret))


def _fernet() -> Fernet:
    return _fernet_for_key(settings.app_secret_key)


def encrypt_json(data: dict, secret_key: str | None = None) -> bytes:
    """secret_key overrides settings.app_secret_key — used only by the
    rotate-secret-key CLI tool to encrypt under a *new* key before that key
    is actually the one configured in the running environment.
    """
    payload = json.dumps(data).encode("utf-8")
    fernet = _fernet_for_key(secret_key) if secret_key is not None else _fernet()
    return fernet.encrypt(payload)


def decrypt_json(token: bytes) -> dict:
    try:
        payload = _fernet().decrypt(token)
    except InvalidToken:
        try:
            payload = Fernet(_derive_key_legacy(settings.app_secret_key)).decrypt(token)
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


def _stored_password_hash(db: Session) -> str | None:
    cred = db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one_or_none()
    if not cred or not cred.encrypted_payload:
        return None
    return decrypt_json(cred.encrypted_payload).get("password_hash")


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
