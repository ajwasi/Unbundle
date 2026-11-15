from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SourceHealth(Base):
    """Whether an external source last worked — one row per source.

    For sources you log in to, `Credential.status`/`last_error` already say
    this. This table is for the ones with no credential to hang it on — the
    public Humble storefront scrape above all, which has broken whenever
    Humble changed its markup — so every source ends up with the same
    "last worked / last failed / why" answer (see services/source_health.py,
    which presents both side by side).
    """

    __tablename__ = "source_health"

    source: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
