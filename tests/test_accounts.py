from app import accounts
from app.models.account_settings import AccountSettings


def test_get_or_create_account_settings_creates_default_row(db):
    cfg = accounts.get_or_create_account_settings(db)
    assert cfg.id == 1
    assert cfg.email is None


def test_get_or_create_account_settings_returns_existing_row(db):
    db.add(AccountSettings(id=1, email="person@example.com"))
    db.commit()

    cfg = accounts.get_or_create_account_settings(db)
    assert cfg.email == "person@example.com"


def test_resolve_identity_label_prefers_session_email(db):
    label = accounts.resolve_identity_label({"email": "person@example.com", "name": "Person", "sub": "abc"}, db)
    assert label == "person@example.com"


def test_resolve_identity_label_falls_back_to_session_name_without_email(db):
    label = accounts.resolve_identity_label({"name": "Person Example", "sub": "abc"}, db)
    assert label == "Person Example"


def test_resolve_identity_label_falls_back_to_session_sub_without_email_or_name(db):
    label = accounts.resolve_identity_label({"sub": "abc"}, db)
    assert label == "abc"


def test_resolve_identity_label_uses_account_settings_email_without_session(db):
    db.add(AccountSettings(id=1, email="admin@example.com"))
    db.commit()

    assert accounts.resolve_identity_label(None, db) == "admin@example.com"


def test_resolve_identity_label_prefers_session_over_account_settings_email(db):
    db.add(AccountSettings(id=1, email="admin@example.com"))
    db.commit()

    label = accounts.resolve_identity_label({"email": "sso-person@example.com"}, db)
    assert label == "sso-person@example.com"


def test_resolve_identity_label_falls_back_to_generic_label_when_nothing_set(db):
    assert accounts.resolve_identity_label(None, db) == "Account"
