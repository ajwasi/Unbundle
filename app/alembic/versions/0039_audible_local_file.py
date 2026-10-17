"""audible_local_file table

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audible_local_file",
        sa.Column("asin", sa.String(length=20), primary_key=True),
        sa.Column("path", sa.String(length=1000), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="download"),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
    )
    # Books downloaded before this table existed: remember the latest
    # completed download's file for each, so they count as "on disk" straight
    # away rather than only after being downloaded (or scanned) again.
    op.execute(
        """
        INSERT INTO audible_local_file (asin, path, size_bytes, source, recorded_at)
        SELECT d.asin, d.downloaded_path, COALESCE(d.expected_size_bytes, 0), 'download',
               COALESCE(d.completed_at, d.queued_at)
        FROM audible_book_download d
        WHERE d.status = 'completed' AND d.downloaded_path <> ''
          AND d.id = (
            SELECT MAX(d2.id) FROM audible_book_download d2
            WHERE d2.asin = d.asin AND d2.status = 'completed' AND d2.downloaded_path <> ''
          )
        """
    )


def downgrade() -> None:
    op.drop_table("audible_local_file")
