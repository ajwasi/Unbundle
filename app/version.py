"""Version identity and update checking.

Tracks the exact git tag when running a tagged release build (see
.github/workflows/docker-publish.yml, which only ever builds and publishes
an image from a pushed vX.Y.Z tag) — falling back to the short commit SHA
for anything else (a plain main build, local dev), since most commits are
never tagged at all. "Latest available" means the highest vX.Y.Z tag that
exists in this repo, not the latest commit on main — pushing to main alone
never publishes an image, so comparing against a commit that was never
actually released would be misleading.
"""

import asyncio
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_REPO = "ajwasi/Unbundle"
_UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
_VERSION_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# None = not checked yet (or the check failed) — deliberately distinct from False,
# so a network hiccup never renders as a false "you're up to date".
_update_available: bool | None = None
_latest_tag: str | None = None
# Set whenever a real network check is actually attempted (never for a
# non-tagged build — see is_tagged_build(), which the template uses to hide
# the manual check button entirely for those) — lets the button show
# "Checked just now" even when the answer is "no update", distinct from
# is_update_available()'s bare True/False.
_last_checked_at: datetime | None = None


def _git_describe_or_sha(cwd: Path) -> str | None:
    """The exact tag if HEAD is precisely one (a real release build), else the
    short commit SHA (everything else — a plain main build, local dev)."""
    for argv in (["git", "describe", "--tags", "--exact-match", "HEAD"], ["git", "rev-parse", "--short", "HEAD"]):
        try:
            result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def get_version() -> str:
    """The VERSION file is baked in at Docker build time (see Dockerfile's `version`
    build stage — `.git` is excluded from the runtime image entirely). Falling back
    to a live git lookup covers local/non-Docker dev, where `.git` is really on disk.
    """
    if _VERSION_FILE.exists():
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value

    return _git_describe_or_sha(Path(__file__).resolve().parent.parent) or "unknown"


def is_update_available() -> bool:
    return bool(_update_available)


def latest_tag() -> str | None:
    """The highest vX.Y.Z tag seen by the last successful check_for_update() call
    — None before the first check, or if it never finds a real tag. The template
    uses this to link straight to that tag's page on GitHub."""
    return _latest_tag


def is_tagged_build() -> bool:
    """False for a plain main/local-dev build (version is a bare commit SHA) —
    there's nothing meaningful to check an update against in that case, so the
    template uses this to hide the manual "Check for updates" button entirely
    rather than offering a control that can never do anything.
    """
    return bool(_VERSION_TAG_RE.match(get_version()))


def last_checked_at() -> datetime | None:
    """When a real update check last actually ran (network attempted) — None
    before the first one, or for a non-tagged build, which never checks at
    all. Distinct from is_update_available(): lets a manual check confirm
    "yes, this just ran" even when the answer was "no update".
    """
    return _last_checked_at


async def check_for_update() -> bool:
    """Returns True if the check itself completed (reached GitHub and got a
    parseable tag list) regardless of whether an update was found — False on
    any failure (network, rate limit, GitHub outage) or if this build isn't a
    tagged release, so the manual check button can tell "no update" apart
    from "the check failed". Never raises either way — a network error,
    GitHub outage, or rate limit should leave _update_available/_latest_tag
    at their previous values, not crash the app or the caller's scheduling
    loop.
    """
    global _update_available, _latest_tag, _last_checked_at

    current = get_version()
    if not _VERSION_TAG_RE.match(current):
        return False

    _last_checked_at = datetime.utcnow()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.github.com/repos/{_REPO}/tags", params={"per_page": 100})
            resp.raise_for_status()
            tags = [t["name"] for t in resp.json() if _VERSION_TAG_RE.match(t["name"])]
    except Exception:
        print("WARNING: could not check for updates (GitHub unreachable or rate-limited)", file=sys.stderr)
        return False

    if not tags:
        return False

    # Tuple-of-ints, not a plain string max — "v0.10.0" must outrank "v0.9.0",
    # which a lexicographic string comparison would get backwards (comparing
    # "1" against "9" character-by-character).
    latest = max(tags, key=lambda t: tuple(int(part) for part in _VERSION_TAG_RE.match(t).groups()))
    _latest_tag = latest
    _update_available = latest != current
    return True


async def run_update_check_loop() -> None:
    while True:
        await check_for_update()
        await asyncio.sleep(_UPDATE_CHECK_INTERVAL_SECONDS)
