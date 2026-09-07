from app.config import DEFAULT_SECRET_KEY
from app.main import _startup_warnings


def test_no_warnings_when_properly_configured(db):
    # conftest.py sets a real (non-default) APP_SECRET_KEY and a real APP_PASSWORD.
    assert _startup_warnings() == []


def test_warns_on_default_secret_key(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_secret_key", DEFAULT_SECRET_KEY)
    warnings = _startup_warnings()
    assert any("APP_SECRET_KEY" in w for w in warnings)


def test_warns_when_no_auth_configured(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_password", "")
    warnings = _startup_warnings()
    assert any("no authentication" in w.lower() for w in warnings)


def test_warns_on_both_simultaneously(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.app_secret_key", DEFAULT_SECRET_KEY)
    monkeypatch.setattr("app.config.settings.app_password", "")
    warnings = _startup_warnings()
    assert len(warnings) == 2


def test_no_auth_warning_when_oidc_enabled_instead(db, monkeypatch):
    from app.models.credential import SOURCE_OIDC, STATUS_OK, Credential
    from app.security import encrypt_json

    monkeypatch.setattr("app.config.settings.app_password", "")
    db.add(
        Credential(
            source=SOURCE_OIDC,
            encrypted_payload=encrypt_json({"issuer": "https://x", "client_id": "c", "client_secret": "s", "enabled": True, "disable_password": False}),
            status=STATUS_OK,
        )
    )
    db.commit()
    assert _startup_warnings() == []
