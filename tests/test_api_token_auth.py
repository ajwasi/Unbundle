"""AuthMiddleware's bearer-token path (app/deps.py) and its CSRF interaction
(app/csrf.py) — needs raw_client (no require_csrf override, see
tests/test_csrf.py's own docstring) since these tests exist specifically to
prove real enforcement/bypass behavior, not route around it like most of the
suite does.
"""

from app.api_tokens import create_token


def test_valid_bearer_token_reaches_a_session_gated_route_with_no_cookie(raw_client, db):
    _row, raw = create_token(db, "My Script")
    resp = raw_client.get("/settings", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200


def test_missing_bearer_token_behaves_exactly_as_before(raw_client):
    resp = raw_client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_invalid_bearer_token_gets_401_json_not_a_redirect(raw_client):
    resp = raw_client.get("/settings", headers={"Authorization": "Bearer garbage"}, follow_redirects=False)
    assert resp.status_code == 401
    assert resp.json()["detail"]


def test_revoked_token_no_longer_works(raw_client, db):
    from app.api_tokens import delete_token

    row, raw = create_token(db, "My Script")
    delete_token(db, row.id)
    resp = raw_client.get("/settings", headers={"Authorization": f"Bearer {raw}"}, follow_redirects=False)
    assert resp.status_code == 401


def test_bearer_token_authed_post_skips_csrf(raw_client, db):
    # No prior GET, no csrf cookie, no csrf_token field — a session-cookie
    # request in this exact shape would 403 (see test_csrf.py). The bearer
    # token must bypass that check entirely.
    _row, raw = create_token(db, "My Script")
    resp = raw_client.post(
        "/settings/api-tokens",
        data={"name": "Second Token"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    assert "Second Token" in resp.text


def test_bearer_token_sets_an_identity_label_naming_the_token(raw_client, db):
    _row, raw = create_token(db, "Grafana Scraper")
    resp = raw_client.get("/settings", headers={"Authorization": f"Bearer {raw}"})
    # The distinctive "API token: " prefix (base.html's <summary> user-menu)
    # rules out this just being a match against the token-list table's own
    # bare-name cell.
    assert "<summary>API token: Grafana Scraper</summary>" in resp.text
