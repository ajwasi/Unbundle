from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SOURCE_HUMBLE = "humble"
SOURCE_OIDC = "oidc"  # SSO config: issuer/client_id/client_secret/enabled/disable_password
SOURCE_APP_AUTH = "app_auth"  # optional DB-stored password override — see security.py
SOURCE_STEAM = "steam"  # Steam Web API key + resolved steamid64 — see connectors/steam_connector.py
SOURCE_GOG = "gog"  # OAuth refresh_token — see connectors/gog_connector.py

STATUS_NOT_CONFIGURED = "not_configured"
STATUS_OK = "ok"
STATUS_ERROR = "error"


class Credential(Base):
    __tablename__ = "credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    encrypted_payload: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_NOT_CONFIGURED)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
