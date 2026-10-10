"""gog purchase date and paid price

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gog_game", sa.Column("purchased_at", sa.DateTime(), nullable=True))
    op.add_column("gog_game", sa.Column("paid_price", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("gog_game", "paid_price")
    op.drop_column("gog_game", "purchased_at")
