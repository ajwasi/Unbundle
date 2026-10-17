"""audible missing-by-author books

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audible_author_book",
        sa.Column("author_name", sa.String(length=300), primary_key=True),
        sa.Column("asin", sa.String(length=20), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("authors", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("narrators", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("series_title", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("series_sequence", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("release_date", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("list_price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audible_author_book")
