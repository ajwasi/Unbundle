"""steam_game table

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "steam_game",
        sa.Column("appid", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("playtime_forever_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("img_icon_url", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("appid"),
    )


def downgrade() -> None:
    op.drop_table("steam_game")
