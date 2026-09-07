from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BundleEntitlement(Base):
    """Third-party keys (Steam etc.) granted by a bundle — never downloadable
    files, so these never create a `Download` row (see models/download.py).

    `steam_app_id`/`steam_owned` and `gog_id`/`gog_owned` stay NULL until
    matched against a synced library (see sync/steam_sync.py, sync/gog_sync.py).
    `raw_json` keeps the original key-entry JSON so matching never requires
    re-crawling old bundles.

    Confirmed against the real 550-bundle dev library (2026-09-07): `gog_id`
    is essentially never populated by Humble's own API — only 2 of 1,277
    entitlements are even key_type "gog", and neither carries a gog_id — vs.
    905 of 1,277 having a usable steam_app_id. The column and matching logic
    exist for consistency and for whatever bundles eventually do carry one,
    but don't expect this to surface much today.

    Uniqueness is (gamekey, machine_name, keyindex), NOT key_name — confirmed
    against a real 550-bundle library (2026-09-06) that the same game can be
    granted more than once in one bundle (e.g. a bonus/gift copy) with an
    identical human-readable key_name, which broke an earlier (gamekey,
    key_name) constraint on the very first real refresh.
    """

    __tablename__ = "bundle_entitlement"

    id: Mapped[int] = mapped_column(primary_key=True)
    gamekey: Mapped[str] = mapped_column(ForeignKey("bundle.gamekey"), nullable=False)
    machine_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    keyindex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    key_name: Mapped[str] = mapped_column(String(300), nullable=False)
    redeemed_on_humble: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="")

    steam_app_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    steam_owned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gog_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gog_owned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    __table_args__ = (UniqueConstraint("gamekey", "machine_name", "keyindex", name="uq_entitlement"),)
