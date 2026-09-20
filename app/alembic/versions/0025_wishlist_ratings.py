"""wishlist ratings

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-20

Each store publishes a different metric on a different scale, so each is
stored in its own terms rather than normalised into a shared "rating"
column that would silently mean three things. GOG gets none: its rating
lives in the catalogue API, which ignores a product-id filter, and matching
by title is the fuzzy matching this codebase avoids elsewhere.
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Audible: a five-star average.
    op.add_column("audible_wishlist_item", sa.Column("rating", sa.Float(), nullable=True))
    op.add_column("audible_wishlist_item", sa.Column("rating_count", sa.Integer(), nullable=True))
    # Steam: Metacritic, out of 100.
    op.add_column("steam_wishlist_item", sa.Column("metacritic", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("steam_wishlist_item") as batch_op:
        batch_op.drop_column("metacritic")
    with op.batch_alter_table("audible_wishlist_item") as batch_op:
        batch_op.drop_column("rating_count")
        batch_op.drop_column("rating")
