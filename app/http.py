"""The one place outbound HTTP clients are built.

Every connector opens a short-lived `httpx.AsyncClient` per call, and building
one loads the CA bundle into a fresh TLS context — measured at ~0.5s of CPU
each time, which was most of what a client cost (and most of why the test
suite took minutes). The TLS context is identical for every client, so it is
built once and shared; connection pools stay per-client as before.
"""

import ssl
from functools import lru_cache

import httpx


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    return httpx.create_ssl_context()


def async_client(**kwargs) -> httpx.AsyncClient:
    """`httpx.AsyncClient(**kwargs)`, sharing the process-wide TLS context
    unless the caller supplies its own `verify`."""
    kwargs.setdefault("verify", _ssl_context())
    return httpx.AsyncClient(**kwargs)
