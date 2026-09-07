"""download_job table + download.download_job_id/subproduct_index

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "download_job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gamekey", sa.String(20), sa.ForeignKey("bundle.gamekey"), nullable=False),
        sa.Column("bundle_name", sa.String(300), nullable=False, server_default=""),
        sa.Column("requested_indices", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    # Plain column, no inline FK: SQLite's ALTER TABLE ADD COLUMN can't add a
    # constraint directly (confirmed 2026-09-06 — needs Alembic batch mode,
    # which recreates the table; not worth it for an optional, ORM-level-only
    # reference on a column that's nullable anyway).
    op.add_column("download", sa.Column("download_job_id", sa.Integer(), nullable=True))
    op.add_column("download", sa.Column("subproduct_index", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("download", "subproduct_index")
    op.drop_column("download", "download_job_id")
    op.drop_table("download_job")
