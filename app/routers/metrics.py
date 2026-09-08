"""Prometheus scrape endpoint. Exempt from the session-cookie gate (see
deps.py) since Prometheus can't do an interactive login — the optional
METRICS_TOKEN bearer check below is the only auth this route can have.
"""

import hmac

from fastapi import APIRouter, Header, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.responses import Response

from app.config import settings

router = APIRouter()


@router.get("/metrics")
def metrics_endpoint(authorization: str | None = Header(default=None)):
    if settings.metrics_token:
        expected = f"Bearer {settings.metrics_token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Missing or invalid metrics bearer token")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
