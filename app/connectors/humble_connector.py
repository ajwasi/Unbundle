"""Humble Bundle connector — calls Humble's real (unofficial) HTTP API directly
with httpx, the same endpoints and auth mechanism the real humble-cli binary
uses internally. No CLI subprocess, no output-scraping, for anything in this
file — that fragility is exactly why the earlier prototype's rewrite was
recommended (see plan). humble-cli is only ever invoked by app/downloads/runner.py,
for the actual download step, to reuse its resume/retry logic.

Field names below (subproducts, download_struct, tpkd_dict/all_tpks,
redeemed_key_val) are based on humble-cli's own Go source and this API's
well-known reverse-engineered shape used by several third-party tools — but
Humble's internal API is undocumented and can drift. Plan Phase 0 step 3 is to
capture one real `order/<gamekey>?all_tpkds=true` response and diff it against
the parsing below before trusting it fully; adjust `_parse_bundle` if fields
come back differently than expected.
"""

import asyncio

import httpx

from app.connectors.base import (
    BaseConnector,
    ConnectorAuthError,
    CredentialStatus,
    LogCallback,
    NormalizedBundle,
    NormalizedDownloadItem,
    NormalizedEntitlement,
)

BASE_URL = "https://www.humblebundle.com/api/v1"
SITE_BASE_URL = "https://www.humblebundle.com"
BATCH_SIZE = 10  # matches humble-cli's own batching for /orders


def order_page_url(gamekey: str) -> str:
    """The real humblebundle.com page for one order — downloads AND third-party
    key reveal/redemption both live here. Confirmed reachable (200, and the
    gamekey itself is echoed back in the response, so it's genuinely parsed
    server-side rather than falling back to a generic page) via a logged-out
    curl; the real content behind a login wall was not (and can't be, without
    a real session) verified from this environment.
    """
    return f"{SITE_BASE_URL}/downloads?key={gamekey}"


class HumbleConnector(BaseConnector):
    source_name = "humble"

    def __init__(self, credential_payload: dict):
        super().__init__(credential_payload)
        self._session_key: str = credential_payload.get("session_key", "")

    def _headers(self) -> dict:
        if not self._session_key:
            raise ConnectorAuthError("Humble is not connected yet — paste your session key in Settings.")
        return {"Cookie": f"_simpleauth_sess={self._session_key}"}

    async def check_credentials(self) -> CredentialStatus:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{BASE_URL}/user/order", headers=self._headers())
        except ConnectorAuthError as exc:
            return CredentialStatus(ok=False, message=str(exc))
        except httpx.HTTPError as exc:
            return CredentialStatus(ok=False, message=f"Could not reach Humble Bundle: {exc}")

        if resp.status_code in (401, 403):
            return CredentialStatus(
                ok=False, message="Humble rejected this session key. Copy a fresh one from your browser."
            )
        if resp.status_code != 200:
            return CredentialStatus(ok=False, message=f"Humble check failed: HTTP {resp.status_code}")

        # An expired cookie has been observed by other tools to sometimes return 200
        # with an HTML login page rather than a clean 401 — guard on content-type,
        # not just status code (plan Phase 0 step 5).
        if "application/json" not in resp.headers.get("content-type", ""):
            return CredentialStatus(
                ok=False, message="Unexpected (non-JSON) response from Humble — session key may be expired."
            )
        try:
            resp.json()
        except ValueError:
            return CredentialStatus(ok=False, message="Unexpected response body from Humble.")

        return CredentialStatus(ok=True, message="Connected")

    async def sync(self, log: LogCallback) -> list[NormalizedBundle]:
        async with httpx.AsyncClient(timeout=30) as client:
            headers = self._headers()

            resp = await client.get(f"{BASE_URL}/user/order", headers=headers)
            if resp.status_code in (401, 403):
                raise ConnectorAuthError("Humble session key was rejected — reconnect in Settings.")
            resp.raise_for_status()
            gamekeys = [entry["gamekey"] for entry in resp.json() if entry.get("gamekey")]
            log("info", f"Humble: found {len(gamekeys)} bundle(s)")

            bundles: list[NormalizedBundle] = []
            for i in range(0, len(gamekeys), BATCH_SIZE):
                batch = gamekeys[i : i + BATCH_SIZE]
                # httpx serializes a list value as repeated `gamekeys=a&gamekeys=b&...`
                # query params — Humble's API requires that form. A single comma-joined
                # string (e.g. "gamekeys=a,b,c") is silently treated as one nonexistent
                # gamekey and returns {"a,b,c": null}, confirmed 2026-09-06 against a
                # real account with 550 bundles (every batch failed identically until
                # this was fixed).
                resp = await client.get(
                    f"{BASE_URL}/orders",
                    params={"all_tpkds": "true", "gamekeys": batch},
                    headers=headers,
                )
                if resp.status_code in (401, 403):
                    raise ConnectorAuthError("Humble session key was rejected mid-sync — reconnect in Settings.")
                resp.raise_for_status()
                payload = resp.json()
                # /orders returns {gamekey: order_json, ...} rather than a list.
                for gamekey, order_json in payload.items():
                    try:
                        bundles.append(parse_bundle(gamekey, order_json))
                    except Exception as exc:  # noqa: BLE001 - one malformed bundle shouldn't abort the sync
                        log("warning", f"Humble: failed to parse bundle {gamekey}: {exc}")
                log("info", f"Humble: fetched details for {len(batch)} bundle(s)")

        return bundles


def parse_bundle(gamekey: str, order: dict) -> NormalizedBundle:
    """Public: also called directly by routers/bundles.py to re-derive a bundle's
    downloadable-item list from its cached raw_json at render time, so that view
    and refresh_library()'s DB-writing path can never drift apart.
    """
    product = order.get("product") or {}
    name = product.get("human_name") or order.get("gamekey", gamekey)
    category = product.get("category", "")

    subproducts = order.get("subproducts", []) or []
    downloads: list[NormalizedDownloadItem] = []
    for subproduct_index, subproduct in enumerate(subproducts, start=1):
        item_name = subproduct.get("human_name", "Unknown item")
        for download in subproduct.get("downloads", []) or []:
            for variant in download.get("download_struct", []) or []:
                url = (variant.get("url") or {}).get("web", "")
                if not url:
                    continue
                downloads.append(
                    NormalizedDownloadItem(
                        item_name=item_name,
                        subproduct_index=subproduct_index,
                        file_format=variant.get("name", ""),
                        original_filename=url.rsplit("/", 1)[-1].split("?", 1)[0],
                        source_url=url,
                        expected_size_bytes=int(variant.get("file_size") or 0),
                    )
                )

    entitlements: list[NormalizedEntitlement] = []
    all_tpks = (order.get("tpkd_dict") or {}).get("all_tpks", []) or []
    for key_entry in all_tpks:
        entitlements.append(
            NormalizedEntitlement(
                key_name=key_entry.get("human_name", "Unknown key"),
                machine_name=key_entry.get("machine_name", ""),
                keyindex=int(key_entry.get("keyindex") or 0),
                redeemed_on_source=isinstance(key_entry.get("redeemed_key_val"), str),
                steam_app_id=key_entry.get("steam_app_id") or None,
                gog_id=key_entry.get("gog_id") or None,
                raw_json=key_entry,
            )
        )

    return NormalizedBundle(
        gamekey=gamekey,
        name=name,
        category=category,
        raw_json=order,
        item_count=len(subproducts),
        purchased_at=order.get("created"),
        amount_spent=float(order.get("amount_spent") or 0.0),
        downloads=downloads,
        entitlements=entitlements,
    )
