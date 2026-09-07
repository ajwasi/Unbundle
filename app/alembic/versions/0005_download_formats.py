"""download_job.requested_formats

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("download_job", sa.Column("requested_formats", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("download_job", "requested_formats")
