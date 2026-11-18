"""Persist GitLab polling baselines and observations."""

import sqlalchemy as sa
from alembic import op

revision = "0002_gitlab_poll_state"
down_revision = "0001_durable_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gitlab_poll_states",
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicit data retention plan")
