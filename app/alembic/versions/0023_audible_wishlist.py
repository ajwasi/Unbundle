"""audible wishlist + price history

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audible_wishlist_item",
        sa.Column("asin", sa.String(length=20), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("subtitle", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("authors", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("narrators", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("runtime_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("added_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "audible_wishlist_price",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asin", sa.String(length=20), sa.ForeignKey("audible_wishlist_item.asin"), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audible_wishlist_price_asin", "audible_wishlist_price", ["asin"])


def downgrade() -> None:
    op.drop_index("ix_audible_wishlist_price_asin", table_name="audible_wishlist_price")
    op.drop_table("audible_wishlist_price")
    op.drop_table("audible_wishlist_item")
