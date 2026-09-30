"""chirp audiobook library

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-30

"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chirp_audiobook",
        sa.Column("purchase_id", sa.String(length=50), primary_key=True),
        sa.Column("audiobook_id", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("authors", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("narrators", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("url_path", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("progress_status", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("position_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("playable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("series_name", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("series_number", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("listing_price", sa.Float(), nullable=True),
        sa.Column("discount_price", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chirp_audiobook")
