from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountSettings(Base):
    """Single config row (id=1) — the admin-settable display email for the
    password-auth login path, which (unlike OIDC) verifies a shared secret
    rather than a person, so it has no identity of its own to show in the
    user menu. See app/accounts.py:resolve_identity_label().
    """

    __tablename__ = "account_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
