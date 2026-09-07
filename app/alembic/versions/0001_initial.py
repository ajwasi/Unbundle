"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bundle",
        sa.Column("gamekey", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "credential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False, unique=True),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="not_configured"),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "bundle_entitlement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gamekey", sa.String(20), sa.ForeignKey("bundle.gamekey"), nullable=False),
        sa.Column("machine_name", sa.String(300), nullable=False, server_default=""),
        sa.Column("keyindex", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("key_name", sa.String(300), nullable=False),
        sa.Column("redeemed_on_humble", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("steam_app_id", sa.String(20), nullable=True),
        sa.Column("steam_owned", sa.Boolean(), nullable=True),
        # (gamekey, key_name) was the original uniqueness assumption; confirmed wrong
        # against a real 550-bundle library (2026-09-06) — the same game can be
        # granted more than once in one bundle with an identical human-readable name.
        sa.UniqueConstraint("gamekey", "machine_name", "keyindex", name="uq_entitlement"),
    )

    op.create_table(
        "download",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gamekey", sa.String(20), sa.ForeignKey("bundle.gamekey"), nullable=False),
        sa.Column("bundle_name", sa.String(300), nullable=False, server_default=""),
        sa.Column("item_name", sa.String(300), nullable=False),
        sa.Column("file_format", sa.String(50), nullable=False, server_default=""),
        sa.Column("original_filename", sa.String(300), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("original_download_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_location_type", sa.String(20), nullable=False, server_default="local"),
        sa.Column("current_location_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("gamekey", "item_name", "original_filename", name="uq_download_item"),
    )
    op.create_index("ix_download_status", "download", ["status"])


def downgrade() -> None:
    op.drop_index("ix_download_status", table_name="download")
    op.drop_table("download")
    op.drop_table("bundle_entitlement")
    op.drop_table("credential")
    op.drop_table("bundle")
