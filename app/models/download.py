from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_VERIFYING = "verifying"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

LOCATION_LOCAL = "local"
# LOCATION_SMB = "smb"  # added in a future phase — no migration needed, see downloads/locations.py


class Download(Base):
    """One downloadable subproduct's tracked file. Only ever created for real
    files (subproducts) — third-party keys live in BundleEntitlement instead,
    by construction (see that model's docstring), not by an accidental filter.
    """

    __tablename__ = "download"

    id: Mapped[int] = mapped_column(primary_key=True)
    gamekey: Mapped[str] = mapped_column(ForeignKey("bundle.gamekey"), nullable=False)
    download_job_id: Mapped[int | None] = mapped_column(ForeignKey("download_job.id"), nullable=True)
    bundle_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    item_name: Mapped[str] = mapped_column(String(300), nullable=False)
    subproduct_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_format: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    original_filename: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expected_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_QUEUED)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Set once at verified-completion time, never touched again.
    original_download_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Generic, mutable "where does this file live now" — local today, smb/etc. later,
    # with no schema change needed when that day comes.
    current_location_type: Mapped[str] = mapped_column(String(20), nullable=False, default=LOCATION_LOCAL)
    current_location_path: Mapped[str] = mapped_column(Text, nullable=False, default="")

    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("gamekey", "item_name", "original_filename", name="uq_download_item"),
    )
