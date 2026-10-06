"""chirp wishlist with price history

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-05

"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chirp_wishlist_item",
        sa.Column("url_path", sa.String(length=300), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("authors", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("details_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "chirp_wishlist_price",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url_path", sa.String(length=300), sa.ForeignKey("chirp_wishlist_item.url_path"), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_chirp_wishlist_price_url_path", "chirp_wishlist_price", ["url_path"])


def downgrade() -> None:
    op.drop_index("ix_chirp_wishlist_price_url_path", table_name="chirp_wishlist_price")
    op.drop_table("chirp_wishlist_price")
    op.drop_table("chirp_wishlist_item")
