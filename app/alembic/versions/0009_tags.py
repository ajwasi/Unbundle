"""tag, bundle_tag, item_tag tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "bundle_tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("gamekey", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"]),
        sa.ForeignKeyConstraint(["gamekey"], ["bundle.gamekey"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tag_id", "gamekey", name="uq_bundle_tag"),
    )
    op.create_table(
        "item_tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("machine_name", sa.String(length=300), nullable=False),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tag_id", "machine_name", name="uq_item_tag"),
    )


def downgrade() -> None:
    op.drop_table("item_tag")
    op.drop_table("bundle_tag")
    op.drop_table("tag")
