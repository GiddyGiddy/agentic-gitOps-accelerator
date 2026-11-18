"""Store immutable bundle snapshots and fingerprint-bound approvals."""

import sqlalchemy as sa
from alembic import op

revision = "0004_release_bundles"
down_revision = "0003_traceability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_bundles",
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "bundle_approvals",
        sa.Column("bundle_fingerprint", sa.String(64), sa.ForeignKey("release_bundles.fingerprint"),
                  primary_key=True),
        sa.Column("request_id", sa.String(128), nullable=False, unique=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicit data retention plan")
