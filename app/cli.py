"""Emergency auth recovery — run directly against the DB, no HTTP/session involved,
so it works even if the web UI itself is unreachable or misconfigured (e.g. SSO-only
mode enabled with an unreachable identity provider). Usage, from a shell with access
to the running container or the host running this app directly:

    docker exec -it <container> python -m app.cli disable-oidc
    docker exec -it <container> python -m app.cli set-password

`disable-oidc` turns off SSO-only mode (and SSO entirely) so APP_PASSWORD works again.
`set-password` stores a DB-side password override (see security.py: check_app_password)
that takes precedence over APP_PASSWORD, without needing to edit env vars or restart.
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


COMMANDS = {"disable-oidc": disable_oidc, "set-password": set_password}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: python -m app.cli <{'|'.join(COMMANDS)}>")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
