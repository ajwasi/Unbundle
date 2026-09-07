"""bundle purchased_at/amount_spent

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bundle", sa.Column("purchased_at", sa.DateTime(), nullable=True))
    op.add_column("bundle", sa.Column("amount_spent", sa.Float(), nullable=False, server_default="0"))
    op.create_index("ix_bundle_purchased_at", "bundle", ["purchased_at"])


def downgrade() -> None:
    op.drop_index("ix_bundle_purchased_at", table_name="bundle")
    op.drop_column("bundle", "amount_spent")
    op.drop_column("bundle", "purchased_at")
