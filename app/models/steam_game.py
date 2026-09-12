from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SteamGame(Base):
    """One owned Steam game, from IPlayerService/GetOwnedGames. Refreshed
    wholesale (upsert every row) on each manual sync, same "cached snapshot"
    role Bundle plays for Humble — this is what BundleEntitlement.steam_app_id
    gets matched against to determine BundleEntitlement.steam_owned.
    """

    __tablename__ = "steam_game"

    appid: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    playtime_forever_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Just a hash, not a usable URL by itself — see icon_url below.
    img_icon_url: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def icon_url(self) -> str:
        """Steam's GetOwnedGames only ever returns the bare icon hash, never a
        usable URL — real games have this exact CDN path (long-stable, same
        one Steam's own community pages use), confirmed against Steam's
        public Web API docs.
        """
        if not self.img_icon_url:
            return ""
        return f"https://media.steampowered.com/steamcommunity/public/images/apps/{self.appid}/{self.img_icon_url}.jpg"
