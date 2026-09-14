from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


class AudiblePdfDownload(Base):
    """One attempt to download a single Audible title's companion PDF (course
    material/illustrated-book supplements some titles ship). Deliberately
    NOT Download/DownloadJob — those are shaped around humble-cli's
    gamekey/subproduct-index semantics and a subprocess it shells out to;
    this is a single plain authenticated-URL httpx stream, with nothing to
    reuse from that subsystem beyond the general status-string shape. See
    app/audible/pdf_downloader.py.
    """

    __tablename__ = "audible_pdf_download"

    id: Mapped[int] = mapped_column(primary_key=True)
    asin: Mapped[str] = mapped_column(ForeignKey("audible_book.asin"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_QUEUED)
    progress_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    downloaded_path: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
