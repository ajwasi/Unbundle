"""bundle category/counts

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bundle", sa.Column("category", sa.String(50), nullable=False, server_default=""))
    op.add_column("bundle", sa.Column("subproduct_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("bundle", sa.Column("key_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_bundle_category", "bundle", ["category"])


def downgrade() -> None:
    op.drop_index("ix_bundle_category", table_name="bundle")
    op.drop_column("bundle", "key_count")
    op.drop_column("bundle", "subproduct_count")
    op.drop_column("bundle", "category")
