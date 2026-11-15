"""Time helpers.

`datetime.utcnow()` is deprecated (it returns a *naive* datetime that merely
looks like UTC). Every timestamp column in this app is a naive-UTC DateTime, so
`utcnow()` keeps exactly that representation — same values, same comparisons,
same DB round-trips — without calling the deprecated API.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (the app's storage convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
