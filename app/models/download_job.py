from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


class DownloadJob(Base):
    """One user-triggered 'download these items' action against one bundle —
    a single humble-cli subprocess invocation (see downloads/runner.py), which
    may cover one, several, or all of that bundle's items. `requested_indices`
    is empty for "all" (humble-cli's own default when -i is omitted),
    otherwise a comma-separated list of 1-based subproduct indices.

    Individual per-file results land in `Download` rows (see that model),
    linked back here via download_job_id, created/updated by verifying the
    filesystem after this job's subprocess exits — never by parsing its stdout.
    """

    __tablename__ = "download_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    gamekey: Mapped[str] = mapped_column(ForeignKey("bundle.gamekey"), nullable=False)
    bundle_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    requested_indices: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    requested_formats: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_QUEUED)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
