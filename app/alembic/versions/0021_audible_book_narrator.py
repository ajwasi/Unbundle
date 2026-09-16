"""audible_book.narrator

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audible_book", sa.Column("narrator", sa.String(length=300), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("audible_book") as batch_op:
        batch_op.drop_column("narrator")
