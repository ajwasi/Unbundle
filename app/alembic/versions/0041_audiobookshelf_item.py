"""audiobookshelf_item table

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-20

"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audiobookshelf_item",
        sa.Column("library_item_id", sa.String(length=50), primary_key=True),
        sa.Column("asin", sa.String(length=20), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("author", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audiobookshelf_item_asin", "audiobookshelf_item", ["asin"])


def downgrade() -> None:
    op.drop_index("ix_audiobookshelf_item_asin", table_name="audiobookshelf_item")
    op.drop_table("audiobookshelf_item")
