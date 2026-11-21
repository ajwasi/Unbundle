"""The mock API stays on loopback unless a deployment opts in (the demo
compose service sets MOCK_API_HOST) — it has no authentication."""

from unittest.mock import patch

import runpy


def _run_main(monkeypatch, env_host):
    if env_host is None:
        monkeypatch.delenv("MOCK_API_HOST", raising=False)
    else:
        monkeypatch.setenv("MOCK_API_HOST", env_host)
    with patch("uvicorn.run") as run:
        runpy.run_module("mock_api.main", run_name="__main__")
    return run.call_args.kwargs["host"]


def test_binds_to_loopback_by_default(monkeypatch):
    assert _run_main(monkeypatch, None) == "127.0.0.1"


def test_host_can_be_overridden_for_the_compose_service(monkeypatch):
    assert _run_main(monkeypatch, "0.0.0.0") == "0.0.0.0"  # noqa: S104 - what the compose file sets
