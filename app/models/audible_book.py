from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudibleBook(Base):
    """One owned Audible title, from GET /1.0/library. Same full-replace-on-
    refresh caching role SteamGame/GogGame play for their own libraries — see
    connectors/audible_connector.py for the real request shape.
    """

    __tablename__ = "audible_book"

    asin: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    runtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cover_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
