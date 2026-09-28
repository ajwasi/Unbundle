"""amazon_music_track_isrc

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("amazon_music_track", sa.Column("isrc", sa.String(length=20), nullable=False, server_default=""))
    op.add_column("amazon_music_track", sa.Column("purchased_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("amazon_music_track", "purchased_at")
    op.drop_column("amazon_music_track", "isrc")
