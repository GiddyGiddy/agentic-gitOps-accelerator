import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.storage.db import create_database
from app.storage.inbox import enqueue
from app.storage.migrate import migrate
from app.storage.models import Approval, AuditEvent, InboxEvent, OutboxAction, Release
from app.supervisor.graph import build_graph
from app.worker import dispatch_one, process_one, reject_event, retry_event
from config.settings import Settings


def test_migration_adopts_existing_tables_and_is_repeatable(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine, factory = create_database(url)
    for table in (AuditEvent.__table__, Release.__table__, Approval.__table__):
        table.create(engine)
    with factory.begin() as session:
        session.add(Release(version="legacy", project_id=1))
    migrate(url)
    migrate(url)
    with factory() as session:
        assert session.get(Release, "legacy").project_id == 1
        assert session.scalars(select(InboxEvent)).all() == []
    engine.dispose()


def test_failed_release_blocks_later_events_but_not_other_threads(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, teams_approvers="manager")
    graph = build_graph(InMemorySaver())
    approval = {"kind": "approval", "payload": {"project_id": 1, "release": "1.0", "wave": "wave2",
                                                  "actor": "manager", "request_id": "a"}}
    tag = {"kind": "tag", "payload": {"project_id": 1, "ref": "refs/tags/REL/1.0"}}
    bad, _ = enqueue(factory, "teams", "a", approval)
    after, _ = enqueue(factory, "gitlab", "b", tag)
    other, _ = enqueue(factory, "argocd", "c", {"kind": "argocd", "payload": {"application": "app"}})
    assert process_one(factory, graph, settings)
    assert process_one(factory, graph, settings)
    assert not process_one(factory, graph, settings)
    with factory() as session:
        assert session.get(InboxEvent, bad.id).status == "failed"
        assert session.get(InboxEvent, after.id).status == "pending"
        assert session.get(InboxEvent, other.id).status == "completed"
    retry_event(factory, bad.id)
    with factory() as session:
        assert session.get(InboxEvent, bad.id).status == "pending"
    assert process_one(factory, graph, settings)
    reject_event(factory, bad.id, "Approval arrived before its release")
    assert process_one(factory, graph, settings)
    with factory() as session:
        assert session.get(InboxEvent, after.id).status == "completed"
    engine.dispose()


def test_outbox_uncertain_send_is_not_replayed(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, write_enabled=True, gitlab_url="https://gitlab.example", gitlab_token="token")
    with factory.begin() as session:
        session.add(OutboxAction(event_id=1, action_key="mr-comment", kind="gitlab_comment",
                                 payload={"project_id": 1, "iid": 2, "body": "text"}))

    def fail(*args):
        raise TimeoutError("unknown delivery")

    monkeypatch.setattr("app.worker.GitLabClient.comment", fail)
    assert dispatch_one(factory, settings)
    assert not dispatch_one(factory, settings)
    with factory() as session:
        assert session.scalar(select(OutboxAction)).status == "uncertain"
    engine.dispose()


def test_delivery_id_must_not_be_reused_for_another_payload(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    engine, factory = create_database(url)
    event = {"kind": "argocd", "payload": {"application": "one"}}
    enqueue(factory, "argocd", "same", event)
    with pytest.raises(ValueError):
        enqueue(factory, "argocd", "same", {"kind": "argocd", "payload": {"application": "two"}})
    engine.dispose()
