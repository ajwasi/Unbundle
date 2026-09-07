from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GogGame(Base):
    """One owned GOG game, from GET /account/getFilteredProducts. Same
    full-replace-on-refresh caching role SteamGame plays for Steam.
    """

    __tablename__ = "gog_game"

    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    image_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
