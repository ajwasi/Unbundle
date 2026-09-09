"""Runs the real mock_api FastAPI app on a real, OS-assigned local TCP port, in
a background thread — a genuine HTTP server, not an in-process ASGI shortcut,
so tests exercise the exact same request/response code path (headers, status
codes, real httpx round-trip) that production traffic through DEMO_MODE would.
See docker-compose.yml's `demo` profile for how the same app runs for real.
"""

import asyncio
import socket
import threading
import time

import uvicorn

from mock_api.main import app as mock_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockApiServer:
    def __init__(self):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(mock_app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.run(self._server.serve())

    def start(self) -> None:
        self._thread.start()
        # uvicorn.Server sets this once its socket is actually accepting
        # connections — polling it beats a fixed sleep, which would either
        # race on a slow CI box or waste time on a fast one.
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Mock API server did not start within 10s")
            time.sleep(0.01)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
