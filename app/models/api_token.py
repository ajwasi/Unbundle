from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ApiToken(Base):
    """A long-lived bearer credential for programmatic (non-browser) access —
    see app/api_tokens.py. Only ever stores token_hash (a plain SHA-256 of the
    real token); the plaintext exists once, at creation, and is never
    persisted anywhere.
    """

    __tablename__ = "api_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
