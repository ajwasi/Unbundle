"""amazon_music_track_order

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("amazon_music_track", sa.Column("track_number", sa.Integer(), nullable=True))
    op.create_table(
        "amazon_music_album_catalog_sync",
        sa.Column("album_asin", sa.String(length=20), primary_key=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("tracks_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tracks_matched", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("amazon_music_album_catalog_sync")
    op.drop_column("amazon_music_track", "track_number")
