"""amazon_music_track_download_id_key

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # track_asin turned out not to be a per-track identity: a live sync of
    # 10,000 purchased rows produced only 27 distinct values there, each one
    # overwritten by whichever row synced last, while download_id was
    # distinct on every single row. Existing rows are stale under the old
    # key — this table is a sync cache, fully repopulated by the next "Sync
    # Amazon Music" — so this drops and recreates rather than migrating data
    # not worth keeping.
    op.drop_table("amazon_music_track")
    op.create_table(
        "amazon_music_track",
        sa.Column("download_id", sa.String(length=64), primary_key=True),
        sa.Column("track_asin", sa.String(length=20), nullable=False, server_default=""),
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
    op.create_index("ix_amazon_music_track_track_asin", "amazon_music_track", ["track_asin"])
    # The list page groups by album and the download feature resolves a whole
    # album at once; both scan by album_asin, never by primary key.
    op.create_index("ix_amazon_music_track_album_asin", "amazon_music_track", ["album_asin"])


def downgrade() -> None:
    op.drop_index("ix_amazon_music_track_album_asin", table_name="amazon_music_track")
    op.drop_index("ix_amazon_music_track_track_asin", table_name="amazon_music_track")
    op.drop_table("amazon_music_track")
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
        sa.Column("is_compilation", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_index("ix_amazon_music_track_download_id", "amazon_music_track", ["download_id"])
    op.create_index("ix_amazon_music_track_album_asin", "amazon_music_track", ["album_asin"])
