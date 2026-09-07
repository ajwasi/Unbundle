"""Steam Web API — a comparison target for BundleEntitlement.steam_app_id, not a
BaseConnector-shaped bundle source (Steam has no notion of "bundles", just an
owned-games list), so this deliberately doesn't implement that interface.

Confirmed against the real dev library (2026-09-06, 550 real bundles): 905 of
1,277 BundleEntitlement rows already have `steam_app_id` populated straight
from Humble's own API (non-Steam key_types like origin/gog/uplay never do, by
construction — see sync/steam_sync.py). The remaining ~29% are mostly
multi-game combo keys with no single appid to match against; those are left
NULL ("not yet checked") rather than guessed at via name matching.

Every endpoint here requires the same `key` query param (a free per-user key
from steamcommunity.com/dev/apikey) — there is no keyless way to test any of
this against the real API. Response shapes below are Steam's long-stable
public Web API, not reverse-engineered like Humble's, but still unverified
live from this environment for that reason.
"""

import re

import httpx

from app.connectors.base import CredentialStatus

BASE_URL = "https://api.steampowered.com"
_STEAM_ID_RE = re.compile(r"^\d{17}$")
_VANITY_URL_RE = re.compile(r"steamcommunity\.com/id/([^/]+)", re.IGNORECASE)


class SteamGameData:
    def __init__(self, appid: int, name: str, playtime_forever_minutes: int, img_icon_url: str):
        self.appid = appid
        self.name = name
        self.playtime_forever_minutes = playtime_forever_minutes
        self.img_icon_url = img_icon_url


def _extract_vanity_name(raw: str) -> str:
    raw = raw.strip()
    match = _VANITY_URL_RE.search(raw)
    if match:
        return match.group(1)
    return raw.rstrip("/").rsplit("/", 1)[-1]  # tolerate a bare profile URL fragment


async def resolve_steamid(api_key: str, steamid_or_url: str) -> str:
    """Returns a resolved SteamID64 string. Raises ValueError if the input
    can't be resolved (bad vanity name, bad key, or a network problem)."""
    candidate = steamid_or_url.strip()
    if _STEAM_ID_RE.match(candidate):
        return candidate

    vanity = _extract_vanity_name(candidate)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/ISteamUser/ResolveVanityURL/v0001/", params={"key": api_key, "vanityurl": vanity})
    resp.raise_for_status()
    data = (resp.json().get("response")) or {}
    if data.get("success") != 1:
        raise ValueError(f"Could not resolve Steam profile '{vanity}' — {data.get('message', 'not found')}.")
    return data["steamid"]


async def check_credentials(api_key: str, steamid64: str) -> CredentialStatus:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{BASE_URL}/ISteamUser/GetPlayerSummaries/v0002/", params={"key": api_key, "steamids": steamid64})
    except httpx.HTTPError as exc:
        return CredentialStatus(ok=False, message=f"Could not reach Steam: {exc}")

    if resp.status_code == 403:
        return CredentialStatus(ok=False, message="Steam rejected this API key.")
    if resp.status_code != 200:
        return CredentialStatus(ok=False, message=f"Steam check failed: HTTP {resp.status_code}")

    players = (resp.json().get("response") or {}).get("players") or []
    if not players:
        return CredentialStatus(ok=False, message="No Steam profile found for that SteamID.")

    player = players[0]
    if player.get("communityvisibilitystate") != 3:
        return CredentialStatus(
            ok=False,
            message="This Steam profile is private. Set your profile and game details to Public in "
            "Steam's privacy settings so the owned-games list can be read.",
        )
    return CredentialStatus(ok=True, message=f"Connected as {player.get('personaname', steamid64)}")


async def fetch_owned_games(api_key: str, steamid64: str) -> list[SteamGameData]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/IPlayerService/GetOwnedGames/v0001/",
            params={"key": api_key, "steamid": steamid64, "include_appinfo": "true", "include_played_free_games": "true", "format": "json"},
        )
    resp.raise_for_status()
    # A private profile (or one game-details-hidden) comes back as a 200 with an
    # empty {"response": {}} — no error status at all, confirmed Steam API behavior.
    games_json = (resp.json().get("response") or {}).get("games")
    if games_json is None:
        raise ValueError(
            "Steam returned no games — the profile's game details may be set to private. "
            "Re-check Settings > Steam or your Steam privacy settings."
        )
    return [
        SteamGameData(
            appid=g["appid"],
            name=g.get("name") or f"App {g['appid']}",
            playtime_forever_minutes=int(g.get("playtime_forever") or 0),
            img_icon_url=g.get("img_icon_url") or "",
        )
        for g in games_json
    ]
