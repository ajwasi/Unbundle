from app.api_tokens import TOKEN_PREFIX, create_token, delete_token, verify_token
from app.models.api_token import ApiToken


def test_create_token_returns_a_row_and_a_prefixed_plaintext(db):
    row, raw = create_token(db, "My Script")
    assert row.name == "My Script"
    assert raw.startswith(TOKEN_PREFIX)
    assert row.token_hash != raw  # never stores the plaintext itself


def test_create_token_persists_the_row(db):
    create_token(db, "My Script")
    assert db.query(ApiToken).filter(ApiToken.name == "My Script").one_or_none() is not None


def test_verify_token_finds_the_matching_row(db):
    row, raw = create_token(db, "My Script")
    found = verify_token(db, raw)
    assert found is not None
    assert found.id == row.id


def test_verify_token_bumps_last_used_at(db):
    row, raw = create_token(db, "My Script")
    assert row.last_used_at is None

    found = verify_token(db, raw)
    assert found.last_used_at is not None


def test_verify_token_rejects_garbage_input(db):
    assert verify_token(db, "not-a-real-token") is None
    assert verify_token(db, "") is None


def test_verify_token_rejects_a_token_that_was_never_created(db):
    # Right prefix, but a value that was never actually issued.
    assert verify_token(db, TOKEN_PREFIX + "totally-made-up") is None


def test_delete_token_removes_it(db):
    row, raw = create_token(db, "My Script")
    delete_token(db, row.id)
    assert verify_token(db, raw) is None
    assert db.query(ApiToken).count() == 0


def test_delete_token_is_a_noop_for_an_unknown_id(db):
    delete_token(db, 999)  # must not raise
