from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GogGame(Base):
    """One owned GOG product, from GET /account/getFilteredProducts. Same
    full-replace-on-refresh caching role SteamGame plays for Steam. Despite
    the name, this covers every product type the endpoint returns (games and
    movies both) — content_type ("game"/"movie"/"other", see
    connectors/gog_connector.py's CONTENT_TYPE_* constants) distinguishes them.
    """

    __tablename__ = "gog_game"

    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    image_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
