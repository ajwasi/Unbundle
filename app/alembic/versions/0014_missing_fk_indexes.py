"""indexes for gamekey/machine_name lookups that don't benefit from an
existing composite unique index (their column isn't the leading one)

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-10

"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_download_job_gamekey", "download_job", ["gamekey"])
    op.create_index("ix_bundle_tag_gamekey", "bundle_tag", ["gamekey"])
    op.create_index("ix_item_tag_machine_name", "item_tag", ["machine_name"])


def downgrade() -> None:
    op.drop_index("ix_item_tag_machine_name", table_name="item_tag")
    op.drop_index("ix_bundle_tag_gamekey", table_name="bundle_tag")
    op.drop_index("ix_download_job_gamekey", table_name="download_job")
