"""amazon_music_track

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "amazon_music_track",
        sa.Column("track_asin", sa.String(length=20), primary_key=True),
        sa.Column("download_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("artist", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("artist_asin", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("album", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("album_asin", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("duration_display", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("cover_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("cover_url_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("missing_since", sa.DateTime(), nullable=True),
        # server_default="0" rather than sa.false(): SQLite stores booleans as
        # integers and PostgreSQL accepts "0" for a boolean default, so one
        # literal works on both backends without branching on dialect.
        sa.Column("is_compilation", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_index("ix_amazon_music_track_download_id", "amazon_music_track", ["download_id"])
    # The list page groups by album and the download feature resolves a whole
    # album at once; both scan by album_asin, never by primary key.
    op.create_index("ix_amazon_music_track_album_asin", "amazon_music_track", ["album_asin"])


def downgrade() -> None:
    op.drop_index("ix_amazon_music_track_album_asin", table_name="amazon_music_track")
    op.drop_index("ix_amazon_music_track_download_id", table_name="amazon_music_track")
    op.drop_table("amazon_music_track")
