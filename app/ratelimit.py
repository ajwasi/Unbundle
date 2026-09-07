"""In-memory per-process rate limiting — no Redis, consistent with this app's
single-worker, single-process design (see Dockerfile's CMD comment on why a
second uvicorn worker isn't safe here either).

Protects two distinct things: /login (brute-force throttling — timing-safe
comparison alone doesn't limit attempt *rate*) and the various /*/refresh
endpoints (hammering one risks Humble/Steam/GOG rate-limiting or blocking this
app's outbound IP, independent of anything password-related).

Keyed by client IP. This app's threat model is a single-user homelab
deployment, not a public multi-tenant service sitting directly on the
internet — no attempt is made to trust X-Forwarded-For, since doing that
without also controlling/validating the proxy chain just adds a spoofable
header attackers can use to bypass the limiter entirely.
"""

import time

from fastapi import HTTPException, Request

_all_limiters: list["RateLimiter"] = []


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._hits: dict[str, list[float]] = {}
        _all_limiters.append(self)

    def allow(self, key: str, now: float) -> bool:
        window_start = now - self.period_seconds
        hits = [h for h in self._hits.get(key, []) if h >= window_start]
        if len(hits) >= self.max_calls:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def reset(self) -> None:
        self._hits.clear()


def reset_all() -> None:
    for limiter in _all_limiters:
        limiter.reset()


def rate_limit(limiter: RateLimiter, key_prefix: str):
    async def _dependency(request: Request) -> None:
        client_host = request.client.host if request.client else "unknown"
        if not limiter.allow(f"{key_prefix}:{client_host}", time.monotonic()):
            raise HTTPException(
                status_code=429,
                detail="Too many requests — please wait a moment before trying again.",
            )

    return _dependency
