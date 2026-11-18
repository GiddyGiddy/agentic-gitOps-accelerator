from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str] = mapped_column(String(256))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Release(Base):
    __tablename__ = "releases"
    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[int]
    pipeline_id: Mapped[int | None]
    wave: Mapped[str] = mapped_column(String(64), default="pending")
    status: Mapped[str] = mapped_column(String(64), default="created")


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("release", "wave"),)
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    release: Mapped[str] = mapped_column(String(128))
    wave: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    __table_args__ = (UniqueConstraint("source", "delivery_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    delivery_id: Mapped[str] = mapped_column(String(128))
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GitLabPollState(Base):
    __tablename__ = "gitlab_poll_states"
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[int]
    generation: Mapped[int] = mapped_column(default=0)
    observations: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxAction(Base):
    __tablename__ = "outbox_actions"
    __table_args__ = (UniqueConstraint("event_id", "action_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int]
    action_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraceBuild(Base):
    __tablename__ = "trace_builds"
    __table_args__ = (UniqueConstraint("source", "project_id", "job_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(512))
    project_id: Mapped[int]
    project_path: Mapped[str] = mapped_column(String(300))
    job_id: Mapped[int]
    pipeline_id: Mapped[int]
    job_name: Mapped[str] = mapped_column(String(256))
    commit_sha: Mapped[str] = mapped_column(String(64))
    ref: Mapped[str] = mapped_column(String(256))
    chart_name: Mapped[str] = mapped_column(String(256))
    chart_version: Mapped[str] = mapped_column(String(128))
    harbor_url: Mapped[str] = mapped_column(String(512))
    harbor_project: Mapped[str] = mapped_column(String(256))
    harbor_repository: Mapped[str] = mapped_column(String(512))
    harbor_tag: Mapped[str] = mapped_column(String(256))
    digest: Mapped[str] = mapped_column(String(71))
    manifest_path: Mapped[str] = mapped_column(String(512))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraceFix(Base):
    __tablename__ = "trace_fixes"
    __table_args__ = (UniqueConstraint("build_id", "jira_id", "commit_sha", "evidence_path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("trace_builds.id"))
    jira_id: Mapped[str] = mapped_column(String(64), index=True)
    commit_sha: Mapped[str] = mapped_column(String(64))
    evidence_path: Mapped[str] = mapped_column(String(512))


class TraceTag(Base):
    __tablename__ = "trace_tags"
    __table_args__ = (UniqueConstraint("source", "project_id", "event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(512))
    project_id: Mapped[int]
    event_id: Mapped[int]
    name: Mapped[str] = mapped_column(String(256))
    commit_sha: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraceDeployment(Base):
    __tablename__ = "trace_deployments"
    __table_args__ = (UniqueConstraint("event_id", "application", "repository"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(512))
    event_id: Mapped[int]
    project_id: Mapped[int]
    job_id: Mapped[int]
    application: Mapped[str] = mapped_column(String(256))
    chart_version: Mapped[str] = mapped_column(String(128))
    repository: Mapped[str] = mapped_column(String(512))
    artifact_path: Mapped[str] = mapped_column(String(512))
    sync_status: Mapped[str | None] = mapped_column(String(64))
    health_status: Mapped[str | None] = mapped_column(String(64))
    comparison_revision: Mapped[str | None] = mapped_column(String(256))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReleaseBundle(Base):
    __tablename__ = "release_bundles"
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    manifest: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BundleApproval(Base):
    __tablename__ = "bundle_approvals"
    bundle_fingerprint: Mapped[str] = mapped_column(
        ForeignKey("release_bundles.fingerprint"), primary_key=True,
    )
    request_id: Mapped[str] = mapped_column(String(128), unique=True)
    actor: Mapped[str] = mapped_column(String(256))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RollbackPlan(Base):
    __tablename__ = "rollback_plans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    plan: Mapped[dict] = mapped_column(JSON)
    requested_by: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    approved_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    approved_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merge_request_iid: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
