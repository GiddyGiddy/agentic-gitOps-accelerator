"""Store approved rollback plans and single-attempt execution state."""

import sqlalchemy as sa
from alembic import op

revision = "0005_rollback_plans"
down_revision = "0004_release_bundles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rollback_plans",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("request_id", sa.String(128), nullable=False, unique=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("approved_by", sa.String(256)),
        sa.Column("approved_fingerprint", sa.String(64)),
        sa.Column("merge_request_iid", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicit data retention plan")
