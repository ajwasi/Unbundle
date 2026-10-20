from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base

SOURCE_HUMBLE = "humble"
SOURCE_OIDC = "oidc"  # SSO config: issuer/client_id/client_secret/enabled/disable_password
SOURCE_APP_AUTH = "app_auth"  # optional DB-stored password override — see security.py
SOURCE_STEAM = "steam"  # Steam Web API key + resolved steamid64 — see connectors/steam_connector.py
SOURCE_GOG = "gog"  # OAuth refresh_token — see connectors/gog_connector.py
SOURCE_AUDIBLE = "audible"  # audible package's Authenticator.to_dict() — see connectors/audible_connector.py
# A captured showPurchasedTracks request, minus the parts that expire — see
# connectors/amazon_music_template.py. Not a login: the cookies still come
# from SOURCE_AUDIBLE, and the access token is refreshed from config.json on
# every sync. What is stored is the *shape* Amazon's own web client sends,
# which is the only reliable way to satisfy an endpoint whose required fields
# are undocumented.
SOURCE_AMAZON_MUSIC = "amazon_music"
# Plain email+password, encrypted at rest — unlike Audible/GOG there's no
# OAuth to hand off to, and unlike Humble/Amazon Music there's no way to
# reuse an existing connection's cookies either. See connectors/
# chirp_connector.py's own docstring for why a plain credentials POST may
# not even clear Chirp's Cloudflare protection in the first place.
SOURCE_CHIRP = "chirp"
# Plain base_url + api_token (a Bearer token minted in AudiobookShelf's own
# Settings > Users > API Keys) — see connectors/audiobookshelf_connector.py.
# Read-only: this app only ever lists AudiobookShelf's library to flag which
# Audible/Humble titles are already there, never writes to it.
SOURCE_AUDIOBOOKSHELF = "audiobookshelf"
# Plain base_url + X-Plex-Token — see connectors/plex_connector.py. Read-only,
# same role as SOURCE_AUDIOBOOKSHELF above. Plex has no dedicated "audiobook"
# library type (audiobooks are conventionally housed in a plain Music/"artist"
# library, author-as-artist/book-as-album), so unlike AudiobookShelf's
# mediaType marker there is nothing to auto-detect the right library by —
# the library's own section key is a required field here.
SOURCE_PLEX = "plex"
# Plain base_url + username + password — see connectors/navidrome_connector.py
# (Subsonic API, salted per-request, never sent in plaintext). Read-only,
# compared against Amazon Music's own owned-albums page rather than Audible's
# — Navidrome is a music server, not an audiobook one.
SOURCE_NAVIDROME = "navidrome"

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

    @classmethod
    def get(cls, db: Session, source: str) -> "Credential | None":
        return db.query(cls).filter(cls.source == source).one_or_none()

    @classmethod
    def get_or_create(cls, db: Session, source: str) -> "Credential":
        """Adds to the session but does not commit — same contract every
        prior copy of this pattern had, callers commit alongside whatever
        else they're setting on the returned row."""
        cred = cls.get(db, source)
        if cred is None:
            cred = cls(source=source)
            db.add(cred)
        return cred

    @classmethod
    def get_payload(cls, db: Session, source: str) -> dict | None:
        # Local import: app/security.py already imports SOURCE_APP_AUTH/Credential
        # from this module, so importing it back at module level here would be
        # circular — deferred to call time instead, by which point both modules
        # are fully loaded (same pattern app/oidc.py already uses for app.config).
        from app.security import decrypt_json

        cred = cls.get(db, source)
        if cred is None or not cred.encrypted_payload:
            return None
        return decrypt_json(cred.encrypted_payload)

    @classmethod
    def set_status(cls, db: Session, source: str, status: str, error: str | None = None) -> None:
        cred = cls.get(db, source)
        if cred:
            cred.status = status
            cred.last_error = error
            db.commit()
