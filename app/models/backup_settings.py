from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BackupSettings(Base):
    """Single config row (id=1) — schedule/retention need to be editable from the
    Settings page at runtime, same reason OIDC config lives in the DB instead of
    an env var (see oidc.py's own docstring).
    """

    __tablename__ = "backup_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    daily_time_utc: Mapped[str] = mapped_column(String(5), nullable=False, default="03:00")
    retention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
