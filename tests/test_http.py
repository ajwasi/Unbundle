"""app/http.py: one shared TLS context, built once, for every outbound client."""

import ssl

import httpx
import pytest

from app import http


@pytest.mark.asyncio
async def test_clients_share_one_tls_context_and_keep_their_own_settings():
    a = http.async_client(timeout=5, headers={"X-A": "1"})
    b = http.async_client(timeout=9)
    try:
        assert isinstance(a, httpx.AsyncClient)
        assert http._ssl_context() is http._ssl_context()
        assert a.timeout.read == 5 and b.timeout.read == 9
        assert a.headers["X-A"] == "1" and "X-A" not in b.headers
    finally:
        await a.aclose()
        await b.aclose()


@pytest.mark.asyncio
async def test_a_caller_supplied_verify_wins():
    own = ssl.create_default_context()
    client = http.async_client(verify=own)
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_respects_a_patched_httpx_asyncclient(monkeypatch):
    # Several suites swap httpx.AsyncClient for one with a MockTransport.
    made = {}
    original = httpx.AsyncClient

    def fake(**kwargs):
        made.update(kwargs)
        return original(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    client = http.async_client(timeout=3)
    assert made["timeout"] == 3
    assert (await client.get("https://example.test")).status_code == 200
    await client.aclose()
