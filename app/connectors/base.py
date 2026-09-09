"""Common interface for external-source connectors. Mirrors
audiobook-tracker/app/connectors/base.py's shape — only one connector exists
today (Humble), but a phase-2 Steam connector will plug into the same
interface, so sync/refresh.py and any future matcher never branch on source.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Literal

LogCallback = Callable[[str, str], None]  # (level, message) -> None


@dataclass
class NormalizedDownloadItem:
    """One downloadable file (a specific format variant of a subproduct), never
    a third-party key. `subproduct_index` is the 1-based position of this
    file's parent subproduct in the bundle's top-level subproduct list —
    confirmed against the real humble-cli binary (2026-09-06) that this is
    exactly what its `download -i` flag expects, and that the index space
    includes subproducts with zero actual downloadable files (a subproduct can
    be a placeholder/beta-key entry with nothing to fetch).
    """

    item_name: str
    subproduct_index: int
    file_format: str
    original_filename: str
    source_url: str
    expected_size_bytes: int
    # Subproduct's own stable slug (same field catalog.py already keys ItemTag on) —
    # lets a completed download resolve its item-level routing tag. Defaulted so
    # existing call sites/tests that construct this dataclass positionally don't break.
    machine_name: str = ""


@dataclass
class NormalizedEntitlement:
    """One third-party key grant (Steam, GOG, etc.) — never downloadable, tracked
    separately from NormalizedDownloadItem by construction. `raw_json` is kept
    so a Steam/GOG-matching pass never needs to re-fetch old bundles.
    """

    key_name: str
    machine_name: str
    keyindex: int
    redeemed_on_source: bool
    steam_app_id: str | None = None
    gog_id: str | None = None
    raw_json: dict = field(default_factory=dict)


@dataclass
class NormalizedBundle:
    gamekey: str
    name: str
    category: str
    raw_json: dict
    # Top-level "things in this bundle" count (games/ebooks/etc.) — NOT the same
    # as len(downloads), which is flattened to one entry per format/platform
    # variant (a single item with a Windows+Mac installer counts as 2 there).
    item_count: int = 0
    purchased_at: str | None = None  # raw ISO string from order.created; parsed at persist time
    amount_spent: float = 0.0
    downloads: list[NormalizedDownloadItem] = field(default_factory=list)
    entitlements: list[NormalizedEntitlement] = field(default_factory=list)


@dataclass
class CredentialStatus:
    ok: bool
    message: str = ""


class ConnectorAuthError(Exception):
    """Raised when a connector's stored credentials are no longer valid."""


class BaseConnector(ABC):
    source_name: Literal["humble", "steam"]

    def __init__(self, credential_payload: dict):
        self.credential_payload = credential_payload

    @abstractmethod
    async def check_credentials(self) -> CredentialStatus:
        """Cheap connectivity/auth check used by the settings page's Test/Save button."""

    @abstractmethod
    async def sync(self, log: LogCallback) -> list[NormalizedBundle]:
        """Fetch this source's current bundle list. Must not raise for expected,
        per-item problems (log and skip instead) — only raise ConnectorAuthError
        for connector-wide auth failures that should abort the whole sync.
        """
