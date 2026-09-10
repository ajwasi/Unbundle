from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db import Base


class DownloadSettings(Base):
    """Single config row (id=1) — same reasoning as BackupSettings: needs to
    be editable from the Downloads page at runtime, not just an env var that
    requires a container restart to change.
    """

    __tablename__ = "download_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=settings.download_concurrency)
