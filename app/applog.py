"""In-app log capture: a small in-memory ring buffer of recent log records,
viewable and downloadable from Settings — independent of whether the
container's own stdout/stderr ever reaches wherever an operator happens to
be looking. Not OpenTelemetry: app/telemetry.py's OTel setup is deliberately
metrics-only (see its own docstring) — adding an OTel *logs* signal would
mean a whole new LoggerProvider plus an OTLP exporter shipping records out
to an external collector this deployment doesn't have, which solves "get
logs out of the process" but not "review/export them from inside the app",
the actual ask. A plain logging.Handler holding a bounded deque is simpler,
needs no new infrastructure, and can't be silently swallowed by container
log buffering the way stdout/stderr can (see Dockerfile's PYTHONUNBUFFERED
comment for that separate, real issue this was confirmed to also hit).

Captures every logger that propagates to the root logger by default — not
just this app's own code, but any third-party library using the standard
logging module. Confirmed relevant in practice: the `audible` package's own
get_soup() logs an error/warning for every error/warning message embedded
in an Amazon response page (audible/login.py), on every single page it
fetches during login — a real, already-present diagnostic signal that was
previously just discarded, since nothing in this app ever attached a
handler anywhere.
"""

import logging
from collections import deque
from datetime import datetime

_MAX_LINES = 2000
_buffer: deque[str] = deque(maxlen=_MAX_LINES)


class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _buffer.append(self.format(record))
        except Exception:
            pass  # a logging handler must never itself raise


def install() -> None:
    """Call once at process startup (main.py's lifespan). Idempotent-ish in
    practice (only ever called once per process), but guards against
    double-installing a second handler anyway.
    """
    root = logging.getLogger()
    if any(isinstance(h, _RingBufferHandler) for h in root.handlers):
        return

    handler = _RingBufferHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    # audible's own internal loggers (audible.login, audible.auth, etc.) log
    # genuinely useful detail (which page it's on, error/warning messages
    # Amazon's own response embedded) at INFO/DEBUG — bumped explicitly since
    # a library's own logger commonly defaults to NOTSET/WARNING otherwise.
    logging.getLogger("audible").setLevel(logging.DEBUG)


def recent_lines() -> list[str]:
    """Oldest first, matching how a terminal/`docker logs` reads."""
    return list(_buffer)


def as_text() -> str:
    return "\n".join(recent_lines()) + ("\n" if _buffer else "")


def clear() -> None:
    _buffer.clear()


def download_filename() -> str:
    return f"unbundle-logs-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"
