"""add downloadable_item_count to bundle — distinct from subproduct_count,
which includes third-party-key-only placeholders with nothing to download

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-13

"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bundle", sa.Column("downloadable_item_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("bundle", "downloadable_item_count")
