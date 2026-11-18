"""Adopt existing baseline tables and add durable workflow storage."""

import sqlalchemy as sa
from alembic import op

revision = "0001_durable_workflows"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "audit_events" not in existing:
        op.create_table(
            "audit_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("reference", sa.String(256), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "releases" not in existing:
        op.create_table(
            "releases",
            sa.Column("version", sa.String(128), primary_key=True),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("pipeline_id", sa.Integer(), nullable=True),
            sa.Column("wave", sa.String(64), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
        )
    if "approvals" not in existing:
        op.create_table(
            "approvals",
            sa.Column("request_id", sa.String(128), primary_key=True),
            sa.Column("release", sa.String(128), nullable=False),
            sa.Column("wave", sa.String(64), nullable=False),
            sa.Column("actor", sa.String(256), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("release", "wave"),
        )
    op.create_table(
        "inbox_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("source", "delivery_id"),
    )
    op.create_index("ix_inbox_events_thread_id", "inbox_events", ["thread_id"])
    op.create_index("ix_inbox_events_status", "inbox_events", ["status"])
    op.create_table(
        "outbox_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "action_key"),
    )
    op.create_index("ix_outbox_actions_status", "outbox_actions", ["status"])


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicit data retention plan")
