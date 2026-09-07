"""gog_game table + bundle_entitlement.gog_id/gog_owned

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-07

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gog_game",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("image_url", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("product_id"),
    )
    op.add_column("bundle_entitlement", sa.Column("gog_id", sa.String(length=20), nullable=True))
    op.add_column("bundle_entitlement", sa.Column("gog_owned", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("bundle_entitlement", "gog_owned")
    op.drop_column("bundle_entitlement", "gog_id")
    op.drop_table("gog_game")
