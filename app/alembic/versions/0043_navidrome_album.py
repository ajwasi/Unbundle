"""navidrome_album table

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-20

"""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "navidrome_album",
        sa.Column("album_id", sa.String(length=50), primary_key=True),
        sa.Column("name", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("artist", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("navidrome_album")
