"""chirp purchase date and paid price

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-05

"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chirp_audiobook", sa.Column("purchased_at", sa.Date(), nullable=True))
    op.add_column("chirp_audiobook", sa.Column("paid_price", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("chirp_audiobook", "paid_price")
    op.drop_column("chirp_audiobook", "purchased_at")
