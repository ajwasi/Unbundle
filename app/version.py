"""Version identity and update checking.

No release/tagging process exists for this project (pyproject.toml's version is a
static, never-bumped placeholder, no git tags) — tracking by short commit SHA avoids
inventing a manual versioning discipline nobody asked for. "Latest available" means
the latest commit on GitHub's main branch, for the same reason: there's nothing else
to compare against yet.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_REPO = "ajwasi/Unbundle"
_UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60

# None = not checked yet (or the check failed) — deliberately distinct from False,
# so a network hiccup never renders as a false "you're up to date".
_update_available: bool | None = None


def get_version() -> str:
    """The VERSION file is baked in at Docker build time (see Dockerfile's `version`
    build stage — `.git` is excluded from the runtime image entirely). Falling back
    to a live `git rev-parse` covers local/non-Docker dev, where `.git` is really on
    disk.
    """
    if _VERSION_FILE.exists():
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    return "unknown"


def is_update_available() -> bool:
    return bool(_update_available)


async def check_for_update() -> None:
    """Never raises — a network error, GitHub outage, or rate limit should leave
    _update_available at its previous value (or None), not crash the app or the
    caller's scheduling loop.
    """
    global _update_available

    current = get_version()
    if current == "unknown":
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.github.com/repos/{_REPO}/commits/main")
            resp.raise_for_status()
            latest_sha = resp.json()["sha"][: len(current)]
    except Exception:
        print("WARNING: could not check for updates (GitHub unreachable or rate-limited)", file=sys.stderr)
        return

    _update_available = latest_sha != current


async def run_update_check_loop() -> None:
    while True:
        await check_for_update()
        await asyncio.sleep(_UPDATE_CHECK_INTERVAL_SECONDS)
