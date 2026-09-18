import pytest

from app.config import Settings


def _settings(**kw) -> Settings:
    return Settings(app_secret_key="x" * 32, **kw)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, False),
        ({"behind_https_proxy": True}, True),
        ({"ssl_certfile": "/certs/c.pem", "ssl_keyfile": "/certs/k.pem"}, True),
    ],
)
def test_https_enabled_covers_both_ways_tls_can_be_terminated(kwargs, expected):
    assert _settings(**kwargs).https_enabled is expected


def test_ssl_files_missing_reports_only_configured_paths_that_do_not_exist(tmp_path):
    real = tmp_path / "cert.pem"
    real.write_text("x")

    cfg = _settings(ssl_certfile=str(real), ssl_keyfile=str(tmp_path / "nope.pem"))
    assert cfg.ssl_files_missing() == [str(tmp_path / "nope.pem")]

    assert _settings().ssl_files_missing() == []


def test_startup_warns_when_certificate_paths_do_not_exist(db, monkeypatch):
    from app.main import _startup_warnings

    monkeypatch.setattr("app.config.settings.ssl_certfile", "/certs/missing.pem")
    warnings = _startup_warnings()
    assert any("/certs/missing.pem" in w for w in warnings)


def test_startup_warns_when_app_tls_and_proxy_tls_are_both_configured(db, monkeypatch, tmp_path):
    from app.main import _startup_warnings

    cert = tmp_path / "cert.pem"
    cert.write_text("x")
    monkeypatch.setattr("app.config.settings.ssl_certfile", str(cert))
    monkeypatch.setattr("app.config.settings.behind_https_proxy", True)

    assert any("BEHIND_HTTPS_PROXY" in w for w in _startup_warnings())


def test_deployment_card_reports_how_the_request_arrived(authed_client):
    resp = authed_client.get("/settings")
    assert "Deployment" in resp.text
    assert "This page was reached as" in resp.text
    assert "None &mdash; plain HTTP" in resp.text or "None — plain HTTP" in resp.text


def test_deployment_card_flags_untrusted_forwarded_proto(authed_client):
    # The silent failure: the proxy says https, the app still resolved http,
    # so every URL it generates (the OIDC redirect URI above all) is wrong.
    resp = authed_client.get("/settings", headers={"X-Forwarded-Proto": "https"})
    assert "FORWARDED_ALLOW_IPS" in resp.text


def test_deployment_card_quiet_when_nothing_is_misconfigured(authed_client):
    resp = authed_client.get("/settings")
    assert "FORWARDED_ALLOW_IPS" not in resp.text
    assert "BEHIND_HTTPS_PROXY=true" not in resp.text
