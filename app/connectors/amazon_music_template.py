"""Stores the shape of a working showPurchasedTracks request.

Amazon's web player sends a `headers` field of roughly twenty x-amzn-*
entries, and the endpoint's required subset is undocumented. Synthesising it
from config.json produced a bare Tomcat 400; replaying a real captured
request produced 3.5 MB of JSON. So rather than keep guessing which fields
matter, this keeps the captured shape and refreshes only the part that
actually expires.

What is stored:
  * the `headers` field verbatim, with its access token replaced at send time
  * `userHash` verbatim
  * the request path

What is NOT stored: cookies. Those still come from the Audible credential, so
this is not a second login and does not extend how long anything stays valid.

The stored blob does contain device and customer identifiers, which is why it
lives in the same Fernet-encrypted Credential row as every other secret and
is never rendered back to the page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.connectors import amazon_music_connector as amc
from app.connectors.amazon_music_probe import parse_curl
from app.models.credential import SOURCE_AMAZON_MUSIC, Credential
from app.security import encrypt_json

AUTH_FIELD = "x-amzn-authentication"


class TemplateError(Exception):
    """The pasted capture could not be turned into a usable template."""


@dataclass
class SyncTemplate:
    path: str
    headers_field: str  # JSON string of x-amzn-* entries
    user_hash: str
    captured_at: str


def _body_fields(spec: dict) -> dict:
    """The captured body, whichever way the client encoded it.

    JSON is what the real player sends, but a capture pasted from a different
    client (or an older one) may be urlencoded, and rejecting that would be
    unhelpful when the fields are right there either way.
    """
    body = spec.get("body")
    if not body:
        raise TemplateError("That capture has no request body, so there is nothing to learn from it.")
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    from urllib.parse import parse_qs

    pairs = parse_qs(body, keep_blank_values=True)
    if not pairs:
        raise TemplateError("Could not read the request body of that capture.")
    return {k: v[0] for k, v in pairs.items()}


def build_template(curl_text: str, captured_at: str) -> SyncTemplate:
    spec = parse_curl(curl_text)  # also enforces the https amazon/a2z host rule

    path = amc_path(spec["url"])
    if not path.endswith(amc.PURCHASED_TRACKS_PATH):
        raise TemplateError(
            f"That capture is for {path}, not {amc.PURCHASED_TRACKS_PATH}. "
            "Capture the request that loads the Purchased view."
        )

    fields = _body_fields(spec)
    headers_field = fields.get("headers")
    if not isinstance(headers_field, str) or AUTH_FIELD not in headers_field:
        raise TemplateError(
            "That capture's body has no `headers` field carrying x-amzn-authentication, "
            "so it cannot be used as a sync template."
        )
    return SyncTemplate(
        path=path,
        headers_field=headers_field,
        user_hash=fields.get("userHash") or "{}",
        captured_at=captured_at,
    )


def amc_path(url: str) -> str:
    import httpx

    return httpx.URL(url).path


def save_template(db: Session, template: SyncTemplate) -> None:
    cred = Credential.get_or_create(db, SOURCE_AMAZON_MUSIC)
    cred.encrypted_payload = encrypt_json(
        {
            "path": template.path,
            "headers_field": template.headers_field,
            "user_hash": template.user_hash,
            "captured_at": template.captured_at,
        }
    )
    cred.status = "ok"
    cred.last_error = None
    db.commit()


def load_template(db: Session) -> SyncTemplate | None:
    payload = Credential.get_payload(db, SOURCE_AMAZON_MUSIC)
    if not payload or not payload.get("headers_field"):
        return None
    return SyncTemplate(
        path=payload.get("path") or amc.PURCHASED_TRACKS_PATH,
        headers_field=payload["headers_field"],
        user_hash=payload.get("user_hash") or "{}",
        captured_at=payload.get("captured_at") or "",
    )


def clear_template(db: Session) -> bool:
    cred = Credential.get(db, SOURCE_AMAZON_MUSIC)
    if not cred:
        return False
    db.delete(cred)
    db.commit()
    return True


def with_fresh_token(headers_field: str, access_token: str) -> str:
    """Swap the captured access token for a current one.

    The token is the only part of the captured shape that expires — device
    ids, marketplace, weblab flags and the rest stay valid — so this is the
    whole of what a refresh has to do.

    x-amzn-authentication holds a JSON *string*, not an object, so it is
    parsed and re-serialised rather than patched textually; a blind
    string replace would corrupt the envelope the moment Amazon changes it.
    """
    try:
        fields = json.loads(headers_field)
    except ValueError as exc:
        raise TemplateError("The saved sync template is not readable. Save a fresh capture.") from exc
    if not isinstance(fields, dict):
        raise TemplateError("The saved sync template is not readable. Save a fresh capture.")

    raw_auth = fields.get(AUTH_FIELD)
    envelope: dict = {}
    if isinstance(raw_auth, str) and raw_auth:
        try:
            parsed = json.loads(raw_auth)
            if isinstance(parsed, dict):
                envelope = parsed
        except ValueError:
            envelope = {}
    if not envelope:
        envelope = {"interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement"}

    envelope["accessToken"] = access_token
    # expirationMS in the capture describes the *old* token and would be a lie
    # about the new one; dropping it is more honest than inventing a value.
    envelope.pop("expirationMS", None)
    fields[AUTH_FIELD] = json.dumps(envelope)
    return json.dumps(fields)
