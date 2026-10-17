"""audible_book_download table

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audible_book_download",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asin", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("progress_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("downloaded_path", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["asin"], ["audible_book.asin"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audible_book_download_asin", "audible_book_download", ["asin"])


def downgrade() -> None:
    op.drop_index("ix_audible_book_download_asin", table_name="audible_book_download")
    op.drop_table("audible_book_download")
