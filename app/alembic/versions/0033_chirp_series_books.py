"""chirp series books

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-05

"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chirp_series_book",
        sa.Column("series_url", sa.String(length=300), primary_key=True),
        sa.Column("url_path", sa.String(length=300), primary_key=True),
        sa.Column("series_name", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("authors", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("series_number", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("listing_price", sa.Float(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chirp_series_book")
