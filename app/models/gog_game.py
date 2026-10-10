from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GogGame(Base):
    """One owned GOG product, from GET /account/getFilteredProducts. Same
    full-replace-on-refresh caching role SteamGame plays for Steam. Despite
    the name, this covers every product type the endpoint returns (games and
    movies both) — content_type ("game"/"movie"/"other", see
    connectors/gog_connector.py's CONTENT_TYPE_* constants) distinguishes them.

    purchased_at/paid_price come from a second, separate source —
    www.gog.com's own order-history page, behind session-cookie auth rather
    than the OAuth token the rest of this row's data comes from (see
    gog_connector.fetch_order_history) — matched onto this row by title at
    sync time, since that page carries no product id. Both stay None for any
    game whose title never appears there: a bundle purchase (the order
    history lists the bundle's own title, never its contents) or a
    Humble-redeemed key (never a GOG "order" at all). No cookie configured
    at all means these are simply never populated.
    """

    __tablename__ = "gog_game"

    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    image_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
