"""amazon_music_downloads

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "amazon_music_destination",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "amazon_music_download",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("download_id", sa.String(length=64), sa.ForeignKey("amazon_music_track.download_id"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("progress_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("downloaded_path", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_amazon_music_download_download_id", "amazon_music_download", ["download_id"])


def downgrade() -> None:
    op.drop_index("ix_amazon_music_download_download_id", table_name="amazon_music_download")
    op.drop_table("amazon_music_download")
    op.drop_table("amazon_music_destination")
