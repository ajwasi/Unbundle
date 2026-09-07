"""Emergency auth recovery — run directly against the DB, no HTTP/session involved,
so it works even if the web UI itself is unreachable or misconfigured (e.g. SSO-only
mode enabled with an unreachable identity provider). Usage, from a shell with access
to the running container or the host running this app directly:

    docker exec -it <container> python -m app.cli disable-oidc
    docker exec -it <container> python -m app.cli set-password
    docker exec -it <container> python -m app.cli rotate-secret-key

`disable-oidc` turns off SSO-only mode (and SSO entirely) so APP_PASSWORD works again.
`set-password` stores a DB-side password override (see security.py: check_app_password)
that takes precedence over APP_PASSWORD, without needing to edit env vars or restart.
`rotate-secret-key` re-encrypts every stored credential from the currently-configured
APP_SECRET_KEY onto a new one you provide — necessary after a suspected leak, since
every Credential.encrypted_payload is Fernet-derived from that one key and simply
changing it (without this) leaves every stored credential permanently undecryptable.
"""

import getpass
import sys

from app.db import SessionLocal
from app.models.credential import SOURCE_APP_AUTH, SOURCE_OIDC, STATUS_NOT_CONFIGURED, STATUS_OK, Credential
from app.security import decrypt_json, encrypt_json, hash_password


def _disable_oidc() -> bool:
    """Pure DB logic, no I/O — split out from disable_oidc() so tests can call this
    directly. Returns whether OIDC was actually configured (and thus changed)."""
    db = SessionLocal()
    try:
        cred = db.query(Credential).filter(Credential.source == SOURCE_OIDC).one_or_none()
        if not cred or not cred.encrypted_payload:
            return False
        payload = decrypt_json(cred.encrypted_payload)
        payload["enabled"] = False
        payload["disable_password"] = False
        cred.encrypted_payload = encrypt_json(payload)
        cred.status = STATUS_NOT_CONFIGURED
        cred.last_error = None
        db.commit()
        return True
    finally:
        db.close()


def disable_oidc() -> None:
    if _disable_oidc():
        print("SSO disabled and password login re-enabled. No restart needed — try logging in again now.")
    else:
        print("OIDC is not configured — nothing to change. Password login should already work.")


def _set_password(password: str) -> None:
    """Pure DB logic, no I/O — split out from set_password() so tests can call this
    directly without going through the interactive getpass prompt."""
    db = SessionLocal()
    try:
        cred = db.query(Credential).filter(Credential.source == SOURCE_APP_AUTH).one_or_none()
        if cred is None:
            cred = Credential(source=SOURCE_APP_AUTH)
            db.add(cred)
        cred.encrypted_payload = encrypt_json({"password_hash": hash_password(password)})
        cred.status = STATUS_OK
        cred.last_error = None
        db.commit()
    finally:
        db.close()


def set_password() -> None:
    password = getpass.getpass("New password: ")
    if not password:
        print("Refusing to set an empty password.")
        sys.exit(1)
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.")
        sys.exit(1)

    _set_password(password)
    print("Password updated. This overrides APP_PASSWORD immediately — no restart needed.")


def _rotate_secret_key(new_secret_key: str) -> int:
    """Pure DB logic, no I/O — split out from rotate_secret_key() so tests can call
    this directly. Decrypts every stored credential under whatever APP_SECRET_KEY
    is currently configured (decrypt_json already handles both the current and
    legacy KDF, so this works regardless of when a row was last written) and
    re-encrypts it under new_secret_key. Returns how many rows were migrated.
    """
    db = SessionLocal()
    try:
        query = db.query(Credential).filter(Credential.encrypted_payload.isnot(None))
        # A real SQL COUNT, not len() on the materialized rows below — CodeQL's
        # clear-text-logging query kept flagging rotate_secret_key()'s summary
        # print() even after the count was computed before new_secret_key was
        # touched, which points at the row *objects* (each carrying
        # .encrypted_payload) being treated as the sensitive value, not the key
        # argument. This never materializes a Credential object at all, so
        # there's nothing plausibly sensitive for it to have come from.
        count = query.count()
        for cred in query.all():
            data = decrypt_json(cred.encrypted_payload)
            cred.encrypted_payload = encrypt_json(data, secret_key=new_secret_key)
        db.commit()
        return count
    finally:
        db.close()


def rotate_secret_key() -> None:
    new_key = getpass.getpass("New APP_SECRET_KEY: ")
    if not new_key:
        print("Refusing to rotate to an empty key.")
        sys.exit(1)
    confirm = getpass.getpass("Confirm new APP_SECRET_KEY: ")
    if new_key != confirm:
        print("Keys did not match.")
        sys.exit(1)

    count = _rotate_secret_key(new_key)
    print(f"Re-encrypted {count} stored credential(s) under the new key.")
    print()
    print("Do this now, in this exact order:")
    print("  1. Set APP_SECRET_KEY to the value you just entered (env var / .env / compose file).")
    print("  2. Restart the app.")
    print("Until step 1 is done, what was just re-encrypted will NOT decrypt under the still-")
    print("running old key — this command prepares the migration, it can't change the running")
    print("environment for you.")


COMMANDS = {"disable-oidc": disable_oidc, "set-password": set_password, "rotate-secret-key": rotate_secret_key}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: python -m app.cli <{'|'.join(COMMANDS)}>")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
