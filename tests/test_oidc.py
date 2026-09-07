from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.models.credential import SOURCE_OIDC, STATUS_OK, Credential
from app.oidc import discover, get_oidc_config, is_oidc_enabled, is_password_disabled
from app.security import encrypt_json


def _save_oidc(db, **overrides):
    payload = {
        "issuer": "https://auth.example.com",
        "client_id": "client-123",
        "client_secret": "secret-abc",
        "enabled": True,
        "disable_password": False,
    }
    payload.update(overrides)
    db.add(Credential(source=SOURCE_OIDC, encrypted_payload=encrypt_json(payload), status=STATUS_OK))
    db.commit()
    return payload


def test_get_oidc_config_none_when_not_configured(db):
    assert get_oidc_config(db) is None


def test_get_oidc_config_returns_decrypted_payload(db):
    payload = _save_oidc(db)
    assert get_oidc_config(db) == payload


def test_is_oidc_enabled_false_when_not_configured(db):
    assert not is_oidc_enabled(db)


def test_is_oidc_enabled_true_when_enabled(db):
    _save_oidc(db, enabled=True)
    assert is_oidc_enabled(db)


def test_is_oidc_enabled_false_when_explicitly_disabled(db):
    _save_oidc(db, enabled=False)
    assert not is_oidc_enabled(db)


def test_is_password_disabled_requires_both_enabled_and_disable_password(db):
    _save_oidc(db, enabled=True, disable_password=False)
    assert not is_password_disabled(db)


def test_is_password_disabled_true_when_both_set(db):
    _save_oidc(db, enabled=True, disable_password=True)
    assert is_password_disabled(db)


def test_is_password_disabled_false_if_enabled_is_false_even_with_flag_set(db):
    # Safety-critical: disable_password must never take effect on its own.
    _save_oidc(db, enabled=False, disable_password=True)
    assert not is_password_disabled(db)


@pytest.mark.asyncio
async def test_discover_success():
    fake_metadata = {
        "authorization_endpoint": "https://auth.example.com/authorize",
        "token_endpoint": "https://auth.example.com/token",
    }
    mock_response = httpx.Response(200, json=fake_metadata, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
        result = await discover("https://auth.example.com/")
    assert result == fake_metadata


@pytest.mark.asyncio
async def test_discover_raises_on_missing_required_fields():
    incomplete_metadata = {"issuer": "https://auth.example.com"}
    mock_response = httpx.Response(200, json=incomplete_metadata, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError):
            await discover("https://auth.example.com")


@pytest.mark.asyncio
async def test_discover_raises_on_http_error():
    mock_response = httpx.Response(404, request=httpx.Request("GET", "https://x"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(httpx.HTTPStatusError):
            await discover("https://auth.example.com")


@pytest.mark.asyncio
async def test_discover_strips_trailing_slash_from_issuer():
    fake_metadata = {"authorization_endpoint": "a", "token_endpoint": "b"}
    mock_response = httpx.Response(200, json=fake_metadata, request=httpx.Request("GET", "https://x"))
    mock_get = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.get", new=mock_get):
        await discover("https://auth.example.com/")
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://auth.example.com/.well-known/openid-configuration"
