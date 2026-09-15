from app.api_tokens import create_token
from app.models.api_token import ApiToken


def test_settings_page_shows_api_card(authed_client):
    resp = authed_client.get("/settings")
    assert "api-card" in resp.text
    assert "No API tokens yet." in resp.text


def test_settings_page_lists_existing_tokens(authed_client, db):
    create_token(db, "My Script")
    resp = authed_client.get("/settings")
    assert "My Script" in resp.text
    assert "Never" in resp.text  # last_used_at column, unused so far


def test_settings_page_never_shows_a_previously_created_plaintext_again(authed_client, db):
    _row, raw = create_token(db, "My Script")
    resp = authed_client.get("/settings")
    assert raw not in resp.text


def test_create_api_token_shows_the_plaintext_once(authed_client, db):
    resp = authed_client.post("/settings/api-tokens", data={"name": "My Script"})
    assert resp.status_code == 200
    assert "copy it now" in resp.text
    row = db.query(ApiToken).one()
    # The actual stored row never has the plaintext — only its hash — so the
    # only way this substring can appear in the response is the one-time
    # callout rendering the value create_token() just returned.
    assert row.token_hash not in resp.text


def test_create_api_token_persists_a_row(authed_client, db):
    authed_client.post("/settings/api-tokens", data={"name": "My Script"})
    row = db.query(ApiToken).one()
    assert row.name == "My Script"


def test_create_api_token_ignores_a_blank_name(authed_client, db):
    resp = authed_client.post("/settings/api-tokens", data={"name": "   "})
    assert resp.status_code == 200
    assert db.query(ApiToken).count() == 0
    assert "copy it now" not in resp.text


def test_delete_api_token_removes_it(authed_client, db):
    row, _raw = create_token(db, "My Script")
    resp = authed_client.post(f"/settings/api-tokens/{row.id}/delete")
    assert resp.status_code == 200
    assert db.query(ApiToken).count() == 0
    assert "My Script" not in resp.text


def test_delete_api_token_is_a_noop_for_an_unknown_id(authed_client, db):
    resp = authed_client.post("/settings/api-tokens/999/delete")
    assert resp.status_code == 200
