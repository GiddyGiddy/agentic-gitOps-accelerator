from datetime import datetime, timezone

from sqlalchemy import DateTime, JSON, String, Text, UniqueConstraint
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
