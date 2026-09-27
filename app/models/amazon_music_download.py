from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


class AmazonMusicDownload(Base):
    """One attempt to download a single purchased track's audio file.

    Deliberately NOT Download/DownloadJob (humble-cli's subprocess machinery)
    nor a copy of AudiblePdfDownload's fire-immediately model — this queues
    through a single sequential worker (see app/amazon_music/downloader.py)
    because a "download my whole library" action can mean thousands of rows,
    and hitting an undocumented personal-account API that many times at once
    is not something to do concurrently.
    """

    __tablename__ = "amazon_music_download"

    id: Mapped[int] = mapped_column(primary_key=True)
    download_id: Mapped[str] = mapped_column(ForeignKey("amazon_music_track.download_id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_QUEUED)
    progress_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    downloaded_path: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
