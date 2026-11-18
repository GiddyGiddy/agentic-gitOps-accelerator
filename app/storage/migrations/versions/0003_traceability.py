"""Store source-backed traceability observations."""

import sqlalchemy as sa
from alembic import op

revision = "0003_traceability"
down_revision = "0002_gitlab_poll_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trace_builds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("project_path", sa.String(300), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("job_name", sa.String(256), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("ref", sa.String(256), nullable=False),
        sa.Column("chart_name", sa.String(256), nullable=False),
        sa.Column("chart_version", sa.String(128), nullable=False),
        sa.Column("harbor_url", sa.String(512), nullable=False),
        sa.Column("harbor_project", sa.String(256), nullable=False),
        sa.Column("harbor_repository", sa.String(512), nullable=False),
        sa.Column("harbor_tag", sa.String(256), nullable=False),
        sa.Column("digest", sa.String(71), nullable=False),
        sa.Column("manifest_path", sa.String(512), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "project_id", "job_id"),
    )
    op.create_table(
        "trace_fixes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("build_id", sa.Integer(), sa.ForeignKey("trace_builds.id"), nullable=False),
        sa.Column("jira_id", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("evidence_path", sa.String(512), nullable=False),
        sa.UniqueConstraint("build_id", "jira_id", "commit_sha", "evidence_path"),
    )
    op.create_index("ix_trace_fixes_jira_id", "trace_fixes", ["jira_id"])
    op.create_table(
        "trace_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "project_id", "event_id"),
    )
    op.create_table(
        "trace_deployments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("application", sa.String(256), nullable=False),
        sa.Column("chart_version", sa.String(128), nullable=False),
        sa.Column("repository", sa.String(512), nullable=False),
        sa.Column("artifact_path", sa.String(512), nullable=False),
        sa.Column("sync_status", sa.String(64)),
        sa.Column("health_status", sa.String(64)),
        sa.Column("comparison_revision", sa.String(256)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "application", "repository"),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade requires an explicit data retention plan")
