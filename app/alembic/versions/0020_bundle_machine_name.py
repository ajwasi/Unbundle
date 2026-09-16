"""bundle.machine_name

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bundle", sa.Column("machine_name", sa.String(length=200), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("bundle") as batch_op:
        batch_op.drop_column("machine_name")
