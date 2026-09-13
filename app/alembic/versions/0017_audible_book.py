"""audible_book table

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audible_book",
        sa.Column("asin", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("author", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("runtime_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cover_url", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("asin"),
    )


def downgrade() -> None:
    op.drop_table("audible_book")
