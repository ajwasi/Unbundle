"""sync_run table

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("bundle_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("sync_run")
