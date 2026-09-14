"""audible_book metadata columns + audible_pdf_download table

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audible_book", sa.Column("purchase_date", sa.DateTime(), nullable=True))
    op.add_column("audible_book", sa.Column("price_amount", sa.Float(), nullable=True))
    op.add_column("audible_book", sa.Column("price_currency", sa.String(length=3), nullable=False, server_default=""))
    op.add_column("audible_book", sa.Column("series_title", sa.String(length=300), nullable=False, server_default=""))
    op.add_column("audible_book", sa.Column("series_sequence", sa.String(length=20), nullable=False, server_default=""))
    op.add_column("audible_book", sa.Column("rating_average", sa.Float(), nullable=True))
    op.add_column("audible_book", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    op.add_column("audible_book", sa.Column("is_finished", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("audible_book", sa.Column("percent_complete", sa.Float(), nullable=False, server_default="0"))
    op.add_column("audible_book", sa.Column("pdf_url", sa.String(length=500), nullable=False, server_default=""))
    op.add_column("audible_book", sa.Column("benefit_id", sa.String(length=50), nullable=False, server_default=""))

    op.create_table(
        "audible_pdf_download",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asin", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("progress_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("downloaded_path", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["asin"], ["audible_book.asin"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audible_pdf_download_asin", "audible_pdf_download", ["asin"])


def downgrade() -> None:
    op.drop_index("ix_audible_pdf_download_asin", table_name="audible_pdf_download")
    op.drop_table("audible_pdf_download")

    # SQLite needs batch mode for DROP COLUMN (no native support pre-3.35;
    # alembic's batch mode recreates the table under the hood either way) —
    # a no-op wrapper on backends that support it natively, like Postgres.
    with op.batch_alter_table("audible_book") as batch_op:
        batch_op.drop_column("benefit_id")
        batch_op.drop_column("pdf_url")
        batch_op.drop_column("percent_complete")
        batch_op.drop_column("is_finished")
        batch_op.drop_column("description")
        batch_op.drop_column("rating_average")
        batch_op.drop_column("series_sequence")
        batch_op.drop_column("series_title")
        batch_op.drop_column("price_currency")
        batch_op.drop_column("price_amount")
        batch_op.drop_column("purchase_date")
