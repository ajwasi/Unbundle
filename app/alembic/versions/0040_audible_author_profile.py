"""audible author profile (bio/photo) + author_asin on audible_author_book

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-18

"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audible_author_book", sa.Column("author_asin", sa.String(length=20), nullable=False, server_default=""))
    op.create_table(
        "audible_author_profile",
        sa.Column("author_asin", sa.String(length=20), primary_key=True),
        sa.Column("name", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("image_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("bio", sa.Text(), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audible_author_profile")
    op.drop_column("audible_author_book", "author_asin")
