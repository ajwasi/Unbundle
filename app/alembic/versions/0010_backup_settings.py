"""backup_settings table

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("daily_time_utc", sa.String(length=5), nullable=False),
        sa.Column("retention_count", sa.Integer(), nullable=False),
        sa.Column("last_backup_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("backup_settings")
