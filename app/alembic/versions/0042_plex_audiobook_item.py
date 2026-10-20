"""plex_audiobook_item table

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-20

"""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plex_audiobook_item",
        sa.Column("rating_key", sa.String(length=50), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("author", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("plex_audiobook_item")
