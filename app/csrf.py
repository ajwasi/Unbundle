"""Double-submit-cookie CSRF protection for state-changing POSTs.

A synchronizer token stored server-side would need a session table this app
deliberately doesn't have (security.py's session cookie is a stateless signed
token, no session store — see its own docstring). Double-submit needs no
server-side storage either: a random token is set as a cookie, and the same
value must also come back in the request itself (a hidden form field, or an
X-CSRF-Token header for the couple of htmx buttons with no enclosing form).
SameSite=Lax already blocks the classic cross-site *form* POST in modern
browsers, but this covers the remaining gap (older/misconfigured browsers,
and any same-site-but-untrusted-content scenario) with the standard
belt-and-suspenders token check: a cross-site attacker's page can trigger a
request that carries the victim's cookie automatically, but can't read that
cookie's value (browsers enforce same-origin on cookie reads) to also put it
in the form field or header, so a forged request always has a mismatch.
"""

import hmac
import secrets

from fastapi import HTTPException, Request

COOKIE_NAME = "unbundle_csrf"
FIELD_NAME = "csrf_token"
HEADER_NAME = "X-CSRF-Token"


def ensure_csrf_cookie(request: Request) -> str:
    """The token to use for this request/response cycle: whatever's already on
    the incoming request's cookie, or a freshly generated one if there isn't
    one yet. Only returns the value — AuthMiddleware (the one place that sees
    both the request and the eventual response) is responsible for actually
    setting the cookie when this generated a new one.
    """
    existing = request.cookies.get(COOKIE_NAME)
    return existing if existing else secrets.token_urlsafe(32)


async def require_csrf(request: Request) -> None:
    cookie_token = request.cookies.get(COOKIE_NAME)
    submitted = request.headers.get(HEADER_NAME)
    if submitted is None:
        form = await request.form()
        submitted = form.get(FIELD_NAME)
    if not cookie_token or not submitted or not hmac.compare_digest(cookie_token, str(submitted)):
        raise HTTPException(status_code=403, detail="Your session token is out of date — please refresh the page and try again.")
