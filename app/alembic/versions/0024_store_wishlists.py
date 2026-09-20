"""steam + gog wishlists with price history

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-20

"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def _price_columns(owner_column: sa.Column, table: str, fk: str) -> None:
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), primary_key=True),
        owner_column,
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index(f"ix_{table}_owner", table, [fk])


def upgrade() -> None:
    op.create_table(
        "steam_wishlist_item",
        sa.Column("appid", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("header_image", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("developers", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("short_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("added_at", sa.DateTime(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("discount_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
    )
    _price_columns(
        sa.Column("appid", sa.Integer(), sa.ForeignKey("steam_wishlist_item.appid"), nullable=False),
        "steam_wishlist_price",
        "appid",
    )

    op.create_table(
        "gog_wishlist_item",
        sa.Column("product_id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("store_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("details_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
    )
    _price_columns(
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("gog_wishlist_item.product_id"), nullable=False),
        "gog_wishlist_price",
        "product_id",
    )


def downgrade() -> None:
    op.drop_index("ix_gog_wishlist_price_owner", table_name="gog_wishlist_price")
    op.drop_table("gog_wishlist_price")
    op.drop_table("gog_wishlist_item")
    op.drop_index("ix_steam_wishlist_price_owner", table_name="steam_wishlist_price")
    op.drop_table("steam_wishlist_price")
    op.drop_table("steam_wishlist_item")
