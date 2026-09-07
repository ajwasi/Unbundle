from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class SyncRun(Base):
    """One /bundles/refresh attempt. Confirmed necessary against a real 550-bundle
    library (2026-09-06): a full refresh takes over a minute, so it runs as a
    background asyncio task (see sync/refresh.py's start_refresh) rather than
    blocking the request — this row is what the UI polls for status.
    """

    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bundle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
