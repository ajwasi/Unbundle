from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.timeutil import utcnow


class SimpleDestinationMixin:
    """Columns shared by every "a named directory downloads may be written
    into, one flagged default" table — the Audible and Amazon Music
    destinations (see routers/destination_crud.py, which operates on any
    model carrying these). Deliberately not Humble's format/tag-routing
    DownloadDestination: those content types have nothing to route by.

    A mixin, not a shared base table: each subsystem keeps its own table, so
    this changes no schema.
    """

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
