from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


class AudibleBookDownload(Base):
    """One attempt to download a single Audible title's actual audiobook —
    a license request against Audible's own API (content/{asin}/licenserequest,
    confirmed via the `audible` package's own official example script:
    github.com/mkb79/Audible/blob/master/examples/download_books_aaxc.py),
    then the resulting .aaxc remuxed into a plain .m4b via ffmpeg using the
    license's own decrypted key/iv (see book_downloader.py).

    Deliberately NOT Download/DownloadJob (humble-cli's own subprocess
    machinery) — same reasoning AudiblePdfDownload's own docstring already
    gives for the PDF case, this is just one more single-file download/
    convert pipeline with nothing in that subsystem to reuse.
    """

    __tablename__ = "audible_book_download"

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
