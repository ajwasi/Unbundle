"""audible series asin and missing-series books

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-07

"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audible_book", sa.Column("series_asin", sa.String(length=20), nullable=False, server_default=""))
    op.create_table(
        "audible_series_book",
        sa.Column("series_asin", sa.String(length=20), primary_key=True),
        sa.Column("asin", sa.String(length=20), primary_key=True),
        sa.Column("series_title", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("authors", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("narrators", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("sequence", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audible_series_book")
    op.drop_column("audible_book", "series_asin")
