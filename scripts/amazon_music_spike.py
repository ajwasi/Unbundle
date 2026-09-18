"""Standalone probe: can we reach music.amazon.com's private JSON API at all?

Answers exactly two questions and then gets out of the way:

  1. Does GET https://music.amazon.com/config.json return JSON (and does it
     look signed in), or an HTML/WAF challenge?
  2. Does replaying one real captured request from a logged-in browser tab
     still return JSON when httpx sends it instead of Chrome?

Deliberately NOT wired into the app: no router, no model, no import from any
app module except the two needed to read and decrypt the stored Audible
credential. Nothing here is a template for the eventual connector.

PRIVACY: this script never prints a cookie, token, customer id, device id,
email or personal name. It prints key *names*, key *paths*, booleans, and
counts. The optional response dump it writes is redacted by the same rules
(see redact()) and lands in a gitignored directory.

Usage (see README of the PR / the run instructions in the conversation):

    python -m scripts.amazon_music_spike --auth audible
    python -m scripts.amazon_music_spike --auth audible --refresh-cookies
    python -m scripts.amazon_music_spike --auth cookie
    python -m scripts.amazon_music_spike --auth audible --replay scripts/.spike/purchased.curl

Exit codes: 0 = got JSON, 2 = got a challenge/HTML, 1 = script/config error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

CONFIG_URL = "https://music.amazon.com/config.json"

# A plain, honest desktop UA. Not an attempt to defeat fingerprinting — if the
# only thing standing between us and the data is pretending harder to be
# Chrome, this spike has failed and the feature is cancelled (see the brief's
# gate). This exists so we aren't rejected for sending no UA at all.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Where captures are read from and redacted dumps are written to. Overridable
# because inside the container /app is not writable by the unprivileged
# appuser this runs as — /data is (docker-entrypoint.sh chowns it), and it is a
# mounted volume, so output survives and is easy to copy back out.
SPIKE_DIR = Path(os.environ.get("SPIKE_DIR") or (Path(__file__).parent / ".spike"))

# Keys whose values are secrets outright.
SECRET_KEY_RE = re.compile(
    r"(cookie|token|csrf|secret|password|credential|authorization|bearer|apikey|api_key|sessionid|session_id|ubid|at-main|x-main|sid)",
    re.I,
)
# Keys that identify the human or the hardware, as opposed to the music.
# Deliberately narrow: a bare "name"/"title" is almost certainly an album or
# track and is exactly the schema detail we need to see, so it is NOT matched.
IDENTITY_KEY_RE = re.compile(
    r"(customer(id|_id|name)?|deviceid|device_id|devicetype|deviceserial|dsn|email|firstname|lastname|fullname|givenname|surname|username|phone|address|postal|zipcode|marketplaceid)",
    re.I,
)
# Long unbroken opaque strings are tokens, not prose. Titles and descriptions
# contain spaces; an ASIN is ~10 chars, so neither is caught here.
OPAQUE_RE = re.compile(r"^[A-Za-z0-9+/=_.-]{64,}$")


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# Cookie sources
# --------------------------------------------------------------------------


def cookies_from_audible(country: str, refresh: bool) -> dict[str, str]:
    """Option A: mint .amazon.<tld> website cookies from the stored Audible
    refresh token.

    Authenticator.set_website_cookies_for_country() POSTs the refresh token to
    /ap/exchangetoken/cookies and gets back auth_cookies scoped to the Amazon
    *website* domain — confirmed by reading audible/auth.py in the installed
    0.12.0. Whether music.amazon.com honours those is the open question this
    whole script exists to answer.
    """
    try:
        from app.db import SessionLocal
        from app.models.credential import SOURCE_AUDIBLE, Credential
    except Exception as exc:  # pragma: no cover - environment problem, not logic
        _fail(f"could not import the app to read the stored credential: {exc!r}")

    import audible

    db = SessionLocal()
    try:
        payload = Credential.get_payload(db, SOURCE_AUDIBLE)
    finally:
        db.close()

    if not payload:
        _fail(
            "no Audible credential is stored in this database. Connect Audible "
            "in Settings first, or use --auth cookie."
        )

    # dict(payload) because from_dict() pops keys off what it is given — the
    # same defensive copy app/sync/audible_sync.py already makes.
    auth = audible.Authenticator.from_dict(dict(payload))

    existing = dict(auth.website_cookies or {})
    if existing and not refresh:
        print(f"  using {len(existing)} website cookie(s) already stored with the Audible credential")
        print("  (pass --refresh-cookies to mint a fresh set instead)")
        return existing

    print(f"  minting fresh website cookies for country={country!r} ...")
    auth.set_website_cookies_for_country(country)
    minted = dict(auth.website_cookies or {})
    if not minted:
        _fail("set_website_cookies_for_country() returned no cookies")
    print(f"  minted {len(minted)} cookie(s)")
    # Deliberately NOT persisted back to the database: this is a read-only
    # probe, and writing a mutated credential from a spike script is exactly
    # the kind of side effect that makes a failed experiment hard to undo.
    return minted


def cookies_from_header() -> dict[str, str]:
    """Option B: the whole Cookie header, copied from a logged-in
    music.amazon.com tab, via env var or a gitignored file.
    """
    raw = os.environ.get("AMAZON_MUSIC_COOKIE", "").strip()
    source = "$AMAZON_MUSIC_COOKIE"
    if not raw:
        path = SPIKE_DIR / "cookie.txt"
        if not path.exists():
            _fail(
                "set AMAZON_MUSIC_COOKIE, or save the Cookie header to "
                f"{path} (create the directory; it is gitignored)."
            )
        raw = path.read_text(encoding="utf-8").strip()
        source = str(path)

    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    jar: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        jar[name.strip()] = value.strip()
    if not jar:
        _fail(f"no cookies parsed out of {source}")
    print(f"  read {len(jar)} cookie(s) from {source}")
    return jar


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def redact(node: Any, secrets: set[str], inherited: str = "") -> Any:
    """Recursively replace secret/identity *values* with a marker, keeping the
    structure and the key names — which is the entire point of the capture.

    A sensitive key holding an object or array is recursed into rather than
    blanked wholesale: blanking it would destroy exactly the schema shape this
    capture exists to reveal. `inherited` carries the sensitivity down so every
    leaf beneath such a key is still redacted, including bare strings in a list
    that have no key of their own to match on.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if inherited:
                kind = inherited
            elif SECRET_KEY_RE.search(key):
                kind = "secret"
            elif IDENTITY_KEY_RE.search(key):
                kind = "identity"
            else:
                kind = ""
            if kind and not isinstance(value, (dict, list)):
                out[key] = f"<REDACTED:{kind}>" if value not in (None, True, False) else value
            else:
                out[key] = redact(value, secrets, kind)
        return out
    if isinstance(node, list):
        return [redact(v, secrets, inherited) for v in node]
    if inherited and isinstance(node, (str, int, float)) and not isinstance(node, bool):
        return f"<REDACTED:{inherited}>"
    if isinstance(node, str):
        if node and node in secrets:
            return "<REDACTED:value>"
        for secret in secrets:
            if len(secret) >= 8 and secret in node:
                return "<REDACTED:contains-secret>"
        if OPAQUE_RE.match(node):
            return f"<REDACTED:opaque len={len(node)}>"
    return node


def key_paths(node: Any, prefix: str = "", depth: int = 0, limit: int = 3) -> list[str]:
    """Key names only, to a shallow depth — never values."""
    if depth > limit:
        return []
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            found.append(path)
            found.extend(key_paths(value, path, depth + 1, limit))
    elif isinstance(node, list) and node:
        found.extend(key_paths(node[0], f"{prefix}[]", depth + 1, limit))
    return found


def find_signed_in(node: Any) -> bool:
    """True when something that looks like a populated customer id exists
    anywhere in the payload. Reports a boolean; never the value.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if re.search(r"customer(id|_id)", key, re.I) and isinstance(value, str) and value.strip():
                return True
            if find_signed_in(value):
                return True
    elif isinstance(node, list):
        return any(find_signed_in(v) for v in node)
    return False


# --------------------------------------------------------------------------
# Response reporting
# --------------------------------------------------------------------------


def looks_like_challenge(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    if "html" in ctype:
        return True
    head = resp.text[:2000].lower()
    return any(m in head for m in ("<html", "captcha", "robot check", "challenge", "cvf_", "enter the characters"))


def report(label: str, resp: httpx.Response, secrets: set[str], dump_to: Path | None) -> bool:
    """Returns True when the response was usable JSON."""
    print(f"\n--- {label} ---")
    print(f"  HTTP status   : {resp.status_code}")
    print(f"  content-type  : {resp.headers.get('content-type', '(none)')}")
    print(f"  body bytes    : {len(resp.content)}")

    challenge = looks_like_challenge(resp)
    print(f"  WAF/HTML page : {challenge}")

    try:
        data = resp.json()
    except Exception:
        print("  parsed as JSON: False")
        if challenge:
            print("\n  >> This is a challenge page, not the API. Per the brief's gate, stop here.")
        return False

    print("  parsed as JSON: True")
    print(f"  signed in     : {find_signed_in(data)}")

    if isinstance(data, dict):
        print(f"  top-level keys ({len(data)}):")
        for key in sorted(data.keys()):
            print(f"    - {key}")
    else:
        print(f"  top-level is a {type(data).__name__}")

    paths = key_paths(data)
    print(f"  key paths (names only, depth<=3): {len(paths)}")
    for path in paths[:60]:
        print(f"    . {path}")
    if len(paths) > 60:
        print(f"    ... {len(paths) - 60} more")

    if dump_to is not None:
        dump_to.parent.mkdir(parents=True, exist_ok=True)
        dump_to.write_text(json.dumps(redact(data, secrets), indent=2, sort_keys=True), encoding="utf-8")
        print(f"\n  redacted response written to: {dump_to}")
        print("  REVIEW IT before pasting it anywhere.")
    return True


# --------------------------------------------------------------------------
# cURL replay
# --------------------------------------------------------------------------


def parse_curl(text: str) -> dict[str, Any]:
    """Parse a DevTools 'Copy as cURL (bash)' command.

    Only the handful of flags Chrome actually emits. Anything unrecognised is
    ignored rather than guessed at.
    """
    text = text.strip()
    if text.startswith("curl"):
        text = text[4:]
    text = text.replace("\\\n", " ").replace("^\n", " ")

    tokens = shlex.split(text)
    url = ""
    method = ""
    headers: dict[str, str] = {}
    cookie_header = ""
    body: str | None = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            raw = tokens[i + 1]
            if ":" in raw:
                name, value = raw.split(":", 1)
                name, value = name.strip(), value.strip()
                if name.lower() == "cookie":
                    cookie_header = value
                else:
                    headers[name] = value
            i += 2
        elif tok in ("-b", "--cookie") and i + 1 < len(tokens):
            cookie_header = tokens[i + 1]
            i += 2
        elif tok in ("-X", "--request") and i + 1 < len(tokens):
            method = tokens[i + 1].upper()
            i += 2
        elif tok in ("-d", "--data", "--data-raw", "--data-binary") and i + 1 < len(tokens):
            body = tokens[i + 1]
            i += 2
        elif tok in ("-A", "--user-agent") and i + 1 < len(tokens):
            headers["User-Agent"] = tokens[i + 1]
            i += 2
        elif tok == "--url" and i + 1 < len(tokens):
            url = tokens[i + 1]
            i += 2
        elif tok.startswith("-"):
            i += 1  # --compressed, --insecure, etc.
        else:
            if not url:
                url = tok
            i += 1

    if not url:
        raise ValueError("no URL found in the captured cURL command")
    if not method:
        method = "POST" if body is not None else "GET"
    return {"url": url, "method": method, "headers": headers, "cookie_header": cookie_header, "body": body}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auth", choices=("audible", "cookie"), default="audible")
    parser.add_argument("--country", default="us", help="Audible locale country code (default: us)")
    parser.add_argument(
        "--refresh-cookies",
        action="store_true",
        help="mint new website cookies instead of reusing any stored on the credential",
    )
    parser.add_argument("--replay", metavar="FILE", help="a DevTools 'Copy as cURL (bash)' capture to replay")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("Amazon Music spike")
    print(f"  auth mode: {args.auth}")

    if args.auth == "audible":
        jar = cookies_from_audible(args.country, args.refresh_cookies)
    else:
        jar = cookies_from_header()

    print(f"  cookie names present ({len(jar)}): {', '.join(sorted(jar)) or '(none)'}")

    secrets = {v for v in jar.values() if v}

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://music.amazon.com/",
    }

    got_json = False
    with httpx.Client(cookies=jar, headers=headers, timeout=args.timeout, follow_redirects=True) as client:
        try:
            resp = client.get(CONFIG_URL)
        except httpx.HTTPError as exc:
            _fail(f"request to config.json failed: {type(exc).__name__}")

        if str(resp.url) != CONFIG_URL:
            print(f"  NOTE: redirected to a different path: {httpx.URL(resp.url).path}")

        got_json = report(
            "GET /config.json",
            resp,
            secrets,
            SPIKE_DIR / f"config-{stamp}.redacted.json",
        )

        if args.replay:
            capture = Path(args.replay)
            if not capture.exists():
                _fail(f"capture file not found: {capture}")
            try:
                spec = parse_curl(capture.read_text(encoding="utf-8"))
            except ValueError as exc:
                _fail(str(exc))

            print(f"\n  replaying: {spec['method']} {httpx.URL(spec['url']).path}")
            print(f"  captured header names: {', '.join(sorted(spec['headers'])) or '(none)'}")

            replay_jar = dict(jar)
            if spec["cookie_header"]:
                for part in spec["cookie_header"].split(";"):
                    if "=" in part:
                        name, value = part.split("=", 1)
                        replay_jar[name.strip()] = value.strip()
                        if value.strip():
                            secrets.add(value.strip())
                print(f"  (capture supplied its own cookies; using those, {len(replay_jar)} total)")

            replay_headers = {**headers, **spec["headers"]}
            try:
                r2 = client.request(
                    spec["method"],
                    spec["url"],
                    headers=replay_headers,
                    cookies=replay_jar,
                    content=spec["body"],
                )
            except httpx.HTTPError as exc:
                _fail(f"replay request failed: {type(exc).__name__}")

            replay_ok = report(
                f"REPLAY {spec['method']} {httpx.URL(spec['url']).path}",
                r2,
                secrets,
                SPIKE_DIR / f"replay-{stamp}.redacted.json",
            )
            got_json = got_json and replay_ok

    print("\n" + "=" * 60)
    if got_json:
        print("RESULT: got JSON. Paste the output above plus the redacted dump(s).")
        return 0
    print("RESULT: no usable JSON — likely a WAF/challenge. Per the gate, stop.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
