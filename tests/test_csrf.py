"""Exercises app/csrf.py's actual enforcement — tests everywhere else use the
`client`/`authed_client` fixtures, which bypass require_csrf by default (see
conftest.py) so they don't need to know this machinery exists. raw_client
does NOT carry that override, so it's the only place this gets to prove
anything.
"""

import re

from app.csrf import COOKIE_NAME, HEADER_NAME


def test_get_request_sets_csrf_cookie(raw_client):
    resp = raw_client.get("/login")
    assert COOKIE_NAME in resp.cookies


def test_csrf_cookie_is_stable_across_requests(raw_client):
    first = raw_client.get("/login").cookies[COOKIE_NAME]
    second = raw_client.get("/login").cookies.get(COOKIE_NAME)
    # None means "no Set-Cookie on this response" — the client's cookie jar
    # still carries the same value forward either way, but the important
    # thing this guards is that a *new* value was never issued.
    assert second is None or second == first


def test_post_without_token_rejected(raw_client):
    raw_client.get("/login")  # establish the csrf cookie first
    resp = raw_client.post("/login", data={"password": "test-password", "next": "/"})
    assert resp.status_code == 403


def test_post_with_wrong_token_rejected(raw_client):
    raw_client.get("/login")
    resp = raw_client.post(
        "/login", data={"password": "test-password", "next": "/", "csrf_token": "not-the-real-token"}
    )
    assert resp.status_code == 403


def test_post_with_correct_token_succeeds(raw_client):
    resp = raw_client.get("/login")
    token = resp.cookies[COOKIE_NAME]
    resp = raw_client.post(
        "/login", data={"password": "test-password", "next": "/", "csrf_token": token}, follow_redirects=False
    )
    assert resp.status_code == 303


def test_post_with_correct_token_via_header_succeeds(raw_client):
    resp = raw_client.get("/login")
    token = resp.cookies[COOKIE_NAME]
    resp = raw_client.post(
        "/login",
        data={"password": "test-password", "next": "/"},
        headers={HEADER_NAME: token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_post_with_no_cookie_at_all_rejected_even_with_a_guessed_token(raw_client):
    # No prior GET — no cookie was ever issued to this client, so there's
    # nothing a submitted token could ever correctly match.
    resp = raw_client.post(
        "/login", data={"password": "test-password", "next": "/", "csrf_token": "anything-at-all"}
    )
    assert resp.status_code == 403


def test_real_login_page_renders_a_working_token_end_to_end(raw_client):
    # The realistic path: parse the token out of the actual rendered form
    # (not the cookie jar directly) and submit it, proving the template wiring
    # and the validation logic actually agree with each other.
    html = raw_client.get("/login").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "login page did not render a csrf_token hidden field"
    resp = raw_client.post(
        "/login",
        data={"password": "test-password", "next": "/", "csrf_token": match.group(1)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
