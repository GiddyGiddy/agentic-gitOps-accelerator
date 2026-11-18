"""Store short-lived Entra login flows and console sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0006_ui_sessions"
down_revision = "0005_rollback_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(36)),
        sa.Column("csrf_token", sa.String(64)),
        sa.Column("login_flow", sa.JSON()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ui_sessions_expires_at", "ui_sessions", ["expires_at"])


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicit data retention plan")
