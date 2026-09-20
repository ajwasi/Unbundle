"""Reachability probe for music.amazon.com's private JSON API.

Named `_probe`, not `_connector`, on purpose: nothing here parses a library.
It answers one question — does music.amazon.com return JSON to httpx, or a
WAF challenge? — and the real connector only gets written once a captured
response proves there is a schema to write against.

Two things are deliberately *not* done here:

  * Minted cookies are never written back to the stored credential. A probe
    that mutates saved state makes a failed experiment hard to undo.
  * Nothing is retried. This is an undocumented API on a personal account;
    one request per button press, no backoff loops.

Everything that leaves this module is redacted (see `redact`): structure and
key names survive, every secret/identity leaf does not.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.models.credential import SOURCE_AUDIBLE, Credential

CONFIG_URL = "https://music.amazon.com/config.json"

# A plain desktop UA so we aren't rejected merely for sending none. Explicitly
# not an attempt to defeat fingerprinting: if the only thing between us and the
# data is impersonating Chrome harder, this probe has failed and the feature is
# cancelled rather than escalated.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://music.amazon.com/",
    # The real player sends this and we did not. For an API this CORS-aware
    # it is a plausible rejection on its own, and costs nothing to include.
    "Origin": "https://music.amazon.com",
}

SECRET_KEY_RE = re.compile(
    r"(cookie|token|csrf|secret|password|credential|authorization|bearer|apikey|api_key|sessionid|session_id|ubid|at-main|x-main|sid)",
    re.I,
)
# Narrow on purpose: a bare "name"/"title" is almost certainly an album or a
# track, which is exactly the schema detail a capture exists to reveal.
IDENTITY_KEY_RE = re.compile(
    r"(customer(id|_id|name)?|deviceid|device_id|devicetype|deviceserial|dsn|email|firstname|lastname|fullname|givenname|surname|username|phone|address|postal|zipcode|marketplaceid)",
    re.I,
)
# Long unbroken strings are tokens, not prose: titles and descriptions contain
# spaces, and an ASIN is ~10 characters, so neither is caught.
OPAQUE_RE = re.compile(r"^[A-Za-z0-9+/=_.-]{64,}$")
URL_RE = re.compile(r"^https?://", re.I)


def redact_url(value: str) -> str:
    """Keep a URL's host, path and parameter *names*; blank every parameter
    value.

    A pre-signed delivery URL carries the customer id, the order id and a
    signature in its query string, under key names ("url", "downloadUrl") that
    no sensitive-key pattern would ever match. Keeping the names is what makes
    a capture readable — learning that an `isrc` parameter exists at all came
    from reading one — while the values are exactly what must not be rendered.
    """
    try:
        url = httpx.URL(value)
        if not url.query:
            return value
        names = []
        for name, _ in url.params.multi_items():
            if name not in names:
                names.append(name)
        query = "&".join(f"{name}=<REDACTED>" for name in names)
        return f"{url.scheme}://{url.host}{url.path}?{query}"
    except Exception:
        # Unparseable but URL-shaped: blank it rather than guess.
        return "<REDACTED:url>"

MAX_CURL_CHARS = 200_000

# Hosts a pasted capture is allowed to target. a2z.com is not a typo: the web
# player's private API is served from region-prefixed hosts under it (e.g.
# na.mesk.skill.music.a2z.com), while the page itself is on amazon.com.
ALLOWED_REPLAY_DOMAINS = frozenset({"amazon.com", "a2z.com"})


class NotConnectedError(Exception):
    """No Audible credential is stored, so option A has nothing to derive from."""


class ProbeError(Exception):
    """The request could not be made at all (DNS, TLS, timeout)."""


@dataclass
class ProbeResult:
    label: str
    status_code: int
    content_type: str
    byte_count: int
    is_json: bool
    is_challenge: bool
    signed_in: bool
    top_level_keys: list[str] = field(default_factory=list)
    key_paths: list[str] = field(default_factory=list)
    collection_sizes: list[str] = field(default_factory=list)
    record_shapes: list[tuple[str, list[str]]] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    redacted_json: str = ""

    @property
    def ok(self) -> bool:
        return self.is_json and not self.is_challenge


def redact(node: Any, secrets: set[str], inherited: str = "") -> Any:
    """Replace secret/identity *values* with a marker, keeping structure and
    key names.

    A sensitive key holding an object or array is recursed into rather than
    blanked wholesale — blanking it would destroy the schema shape the capture
    exists to reveal. `inherited` carries sensitivity downward so every leaf
    beneath such a key is still redacted, including bare strings in a list that
    have no key of their own to match on.
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
                # Booleans and nulls carry no secret, and keeping them makes a
                # capture far easier to read. isinstance rather than
                # `value in (None, True, False)`, which would also keep the
                # integers 0 and 1 (they compare equal to the bools).
                keep = value is None or isinstance(value, bool)
                out[key] = value if keep else f"<REDACTED:{kind}>"
            else:
                out[key] = redact(value, secrets, kind)
        return out
    if isinstance(node, list):
        return [redact(v, secrets, inherited) for v in node]
    if inherited and isinstance(node, (str, int, float)) and not isinstance(node, bool):
        return f"<REDACTED:{inherited}>"
    if isinstance(node, str):
        if URL_RE.match(node):
            return redact_url(node)
        if node and node in secrets:
            return "<REDACTED:value>"
        for secret in secrets:
            if len(secret) >= 8 and secret in node:
                return "<REDACTED:contains-secret>"
        if OPAQUE_RE.match(node):
            return f"<REDACTED:opaque len={len(node)}>"
    return node


def sample_lists(node: Any, max_items: int = 3) -> Any:
    """Keep only the first few entries of every list.

    A real library response is megabytes of albums, but the schema is fully
    visible in the first two or three — and slicing the *serialised* JSON to a
    byte budget instead would hand back a document cut off mid-structure.
    Sampling keeps the result valid, small, and stops the whole of someone's
    library from being rendered to answer a question about field names.
    """
    if isinstance(node, dict):
        return {k: sample_lists(v, max_items) for k, v in node.items()}
    if isinstance(node, list):
        kept = [sample_lists(v, max_items) for v in node[:max_items]]
        if len(node) > max_items:
            kept.append(f"<{len(node) - max_items} more items omitted>")
        return kept
    return node


def collection_sizes(node: Any, prefix: str = "", out: list[str] | None = None) -> list[str]:
    """Where the bulk actually is: "albums[] = 847". Counts only — the items
    themselves never appear here.
    """
    if out is None:
        out = []
    if isinstance(node, dict):
        for key, value in node.items():
            collection_sizes(value, f"{prefix}.{key}" if prefix else key, out)
    elif isinstance(node, list):
        out.append(f"{prefix or '(root)'}[] = {len(node)}")
        if node:
            collection_sizes(node[0], f"{prefix}[]", out)
    return out


# Field names a music record plausibly carries. Used only to *locate* records
# inside a response — never to assert a schema. showPurchasedTracks returns
# Amazon's server-driven UI format (a `methods[].template.widgets` tree), so
# the tracks are buried many levels below anything a shallow key listing
# reaches, surrounded by page furniture like multiSelectBar and contextMenu.
RECORD_HINT_KEYS = frozenset(
    {
        "asin",
        "albumasin",
        "albumname",
        "album",
        "artist",
        "artistname",
        "artistasin",
        "title",
        "name",
        "isrc",
        "duration",
        "durationseconds",
        "tracknum",
        "tracknumber",
        "objectid",
        "cdoid",
        "contentid",
        "purchased",
        "purchasedate",
        "uploaded",
    }
)


def find_record_nodes(
    node: Any,
    hints: frozenset[str] = RECORD_HINT_KEYS,
    min_hits: int = 3,
    limit: int = 8,
    prefix: str = "",
    found: list[tuple[str, list[str]]] | None = None,
) -> list[tuple[str, list[str]]]:
    """Locate dicts that look like data records, and report their path plus
    their key *names* — never their values.

    A UI-template response hides the useful objects far below the depth a flat
    key listing reaches. Rather than rendering megabytes to find them, this
    reports "an object with these field names lives here", which is exactly
    what designing a table needs and carries nothing sensitive.

    Only the first occurrence of each distinct key-set is kept: a list of 800
    tracks is 800 identical shapes, and one is as informative as all of them.
    """
    if found is None:
        found = []
    if len(found) >= limit:
        return found

    if isinstance(node, dict):
        names = sorted(node.keys())
        hits = sum(1 for n in names if n.lower() in hints)
        if hits >= min_hits:
            shape = [n for n in names]
            if not any(existing == shape for _, existing in found):
                found.append((prefix or "(root)", shape))
        for key, value in node.items():
            find_record_nodes(value, hints, min_hits, limit, f"{prefix}.{key}" if prefix else key, found)
    elif isinstance(node, list):
        for item in node[:3]:  # identical shapes repeat; three is plenty
            find_record_nodes(item, hints, min_hits, limit, f"{prefix}[]", found)
    return found


def referenced_endpoints(node: Any, limit: int = 40) -> list[str]:
    """Every distinct API URL embedded in the response, as path plus parameter
    names.

    In a server-driven UI the payload doesn't just carry data — it carries the
    calls the page can make next, which is how `zipDownloadTracks`,
    `showPurchasedTracks?sortBy=` and `thumbsUp?trackCatalogId=` were all
    discovered by eye. Surfacing them deliberately turns one capture into a map
    of the API, and the pagination call should appear here the moment a
    response contains one.

    Values are already stripped by redact_url; only host, path and parameter
    names survive.
    """
    seen: list[str] = []

    def walk(current: Any) -> None:
        if len(seen) >= limit:
            return
        if isinstance(current, dict):
            for value in current.values():
                walk(value)
        elif isinstance(current, list):
            for value in current:
                walk(value)
        elif isinstance(current, str) and URL_RE.match(current):
            try:
                url = httpx.URL(current)
            except Exception:
                return
            names = sorted({name for name, _ in url.params.multi_items()})
            entry = f"{url.path}" + (f"?{'&'.join(names)}" if names else "")
            if entry not in seen:
                seen.append(entry)

    walk(node)
    return sorted(seen)


def largest_list_shapes(node: Any, limit: int = 5) -> list[tuple[str, list[str]]]:
    """Fallback locator: the longest lists in the response, and the field names
    of their first element.

    RECORD_HINT_KEYS only fires when the payload uses semantic field names. A
    server-driven UI may instead render tracks as generic widget items
    ("primaryText", "secondaryText"), which no hint list should try to guess at.
    But whatever the names, a library's track list is far longer than any piece
    of page furniture — so ranking by length finds it without assuming anything
    about the schema.
    """
    sizes: list[tuple[int, str, list[str]]] = []

    def walk(current: Any, prefix: str) -> None:
        if isinstance(current, dict):
            for key, value in current.items():
                walk(value, f"{prefix}.{key}" if prefix else key)
        elif isinstance(current, list):
            if current and isinstance(current[0], dict):
                sizes.append((len(current), prefix or "(root)", sorted(current[0].keys())))
            if current:
                walk(current[0], f"{prefix}[]")

    walk(node, "")
    sizes.sort(key=lambda row: row[0], reverse=True)
    return [(f"{path}[] ({count} items)", fields) for count, path, fields in sizes[:limit]]


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
    """True when a populated customer-id-shaped value exists anywhere. Returns
    a boolean; the value itself never leaves this function.
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


def cookies_from_audible_credential(db, country: str = "us", refresh: bool = False) -> dict[str, str]:
    """Option A: mint `.amazon.<tld>` website cookies from the stored Audible
    refresh token.

    Authenticator.set_website_cookies_for_country() exchanges the refresh token
    at /ap/exchangetoken/cookies for auth_cookies scoped to the Amazon website
    domain — confirmed by reading audible/auth.py in the installed 0.12.0.
    Whether music.amazon.com honours those is the open question.
    """
    import audible

    payload = Credential.get_payload(db, SOURCE_AUDIBLE)
    if not payload:
        raise NotConnectedError("Audible isn't connected, so there's no Amazon login to derive cookies from.")

    # dict() because from_dict() pops keys off what it is handed — the same
    # defensive copy app/sync/audible_sync.py already makes.
    auth = audible.Authenticator.from_dict(dict(payload))

    existing = dict(auth.website_cookies or {})
    if existing and not refresh:
        return existing

    try:
        auth.set_website_cookies_for_country(country)
    except Exception as exc:
        raise ProbeError(f"Could not mint Amazon website cookies: {type(exc).__name__}") from exc

    minted = dict(auth.website_cookies or {})
    if not minted:
        raise ProbeError("Amazon returned no website cookies for the stored login.")
    return minted


def _looks_like_challenge(resp: httpx.Response) -> bool:
    if "html" in resp.headers.get("content-type", "").lower():
        return True
    head = resp.text[:2000].lower()
    return any(m in head for m in ("<html", "captcha", "robot check", "challenge", "cvf_", "enter the characters"))


def _build_result(label: str, resp: httpx.Response, secrets: set[str]) -> ProbeResult:
    challenge = _looks_like_challenge(resp)
    result = ProbeResult(
        label=label,
        status_code=resp.status_code,
        content_type=resp.headers.get("content-type", ""),
        byte_count=len(resp.content),
        is_json=False,
        is_challenge=challenge,
        signed_in=False,
    )
    try:
        data = resp.json()
    except Exception:
        return result

    result.is_json = True
    result.signed_in = find_signed_in(data)
    if isinstance(data, dict):
        result.top_level_keys = sorted(data.keys())
    result.key_paths = key_paths(data)[:80]
    result.collection_sizes = collection_sizes(data)[:40]
    # Semantic field names first; if the payload names nothing recognisably
    # musical, fall back to ranking lists by length, which assumes no schema
    # at all.
    result.record_shapes = find_record_nodes(data) or largest_list_shapes(data)
    result.endpoints = referenced_endpoints(data)
    # Sample before serialising, so what comes back is valid JSON showing the
    # schema rather than a megabyte of library truncated mid-object. The byte
    # cap stays only as a backstop against a pathologically wide single item.
    result.redacted_json = json.dumps(redact(sample_lists(data), secrets), indent=2, sort_keys=True)[:200_000]
    return result


def probe_config(cookies: dict[str, str], timeout: float = 30.0) -> ProbeResult:
    secrets = {v for v in cookies.values() if v}
    try:
        with httpx.Client(cookies=cookies, headers=BASE_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(CONFIG_URL)
    except httpx.HTTPError as exc:
        raise ProbeError(f"Request to config.json failed: {type(exc).__name__}") from exc
    return _build_result("GET /config.json", resp, secrets)


def parse_curl(text: str) -> dict[str, Any]:
    """Parse a DevTools "Copy as cURL (bash)" command.

    Only the flags Chrome actually emits; anything unrecognised is skipped
    rather than guessed at.
    """
    text = text.strip()
    if len(text) > MAX_CURL_CHARS:
        raise ValueError("That capture is too large to be a single request.")
    if not text:
        raise ValueError("Paste the cURL command copied from DevTools.")
    if text.startswith("curl"):
        text = text[4:]
    text = text.replace("\\\n", " ")

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(
            "Could not parse that command. Use DevTools → Copy as cURL (bash), not the cmd or PowerShell variant."
        ) from exc

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
            i += 1
        else:
            if not url:
                url = tok
            i += 1

    if not url:
        raise ValueError("No URL found in that cURL command.")
    parsed = httpx.URL(url)
    # Keeps a pasted command from being turned into a request to anywhere the
    # app wouldn't otherwise talk to. a2z.com is here because the web player's
    # own API lives on region-prefixed hosts like na.mesk.skill.music.a2z.com,
    # not on amazon.com at all. Matched on a label boundary, not as a bare
    # suffix — "notamazon.com" ends with "amazon.com".
    host = (parsed.host or "").lower()
    if parsed.scheme != "https" or not any(host == d or host.endswith(f".{d}") for d in ALLOWED_REPLAY_DOMAINS):
        allowed = " or ".join(sorted(ALLOWED_REPLAY_DOMAINS))
        raise ValueError(f"That capture points somewhere other than an https {allowed} host.")
    if not method:
        method = "POST" if body is not None else "GET"
    return {"url": url, "method": method, "headers": headers, "cookie_header": cookie_header, "body": body}


def replay_curl(curl_text: str, cookies: dict[str, str], timeout: float = 30.0) -> ProbeResult:
    spec = parse_curl(curl_text)

    jar = dict(cookies)
    for part in spec["cookie_header"].split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            jar[name.strip()] = value.strip()

    secrets = {v for v in jar.values() if v}
    headers = {**BASE_HEADERS, **spec["headers"]}
    try:
        with httpx.Client(cookies=jar, timeout=timeout, follow_redirects=True) as client:
            resp = client.request(spec["method"], spec["url"], headers=headers, content=spec["body"])
    except httpx.HTTPError as exc:
        raise ProbeError(f"Replay request failed: {type(exc).__name__}") from exc

    return _build_result(f"{spec['method']} {httpx.URL(spec['url']).path}", resp, secrets)
