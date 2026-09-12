"""add content_type to gog_game — movies are no longer discarded, this
distinguishes them from games

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-12

"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gog_game", sa.Column("content_type", sa.String(length=20), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("gog_game", "content_type")
