import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import select

from app.gitlab_poller import poll_once
from app.storage.db import create_database
from app.storage.inbox import enqueue
from app.storage.migrate import migrate
from app.storage.models import InboxEvent, Release
from app.supervisor.graph import build_graph
from app.worker import process_one, retry_event
from config.settings import Settings


@pytest.fixture
def postgres():
    url = os.environ.get("GITOPS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set GITOPS_TEST_DATABASE_URL to an isolated empty PostgreSQL database")
    migrate(url)
    engine, factory = create_database(url)
    yield url, engine, factory
    engine.dispose()


def test_postgres_checkpoint_recovery_and_serialization(postgres):
    url, engine, factory = postgres
    settings = Settings(database_url=url, teams_approvers="manager")
    tag = {"kind": "tag", "payload": {"project_id": 81, "ref": "refs/tags/REL/8100.1"}}
    item, _ = enqueue(factory, "gitlab", "pg-tag-1", tag)
    approval = {"kind": "approval", "payload": {
        "project_id": 81, "release": "8100.1", "wave": "wave2",
        "actor": "manager", "request_id": "pg-approval-1",
    }}
    enqueue(factory, "teams", "pg-approval-1", approval)
    started, release_lock = Event(), Event()
    conn_string = url.replace("postgresql+psycopg://", "postgresql://", 1)

    with PostgresSaver.from_conn_string(conn_string) as saver1, PostgresSaver.from_conn_string(conn_string) as saver2:
        real_graph = build_graph(saver1)
        graph2 = build_graph(saver2)

        class PausingGraph:
            def invoke(self, *args, **kwargs):
                started.set()
                assert release_lock.wait(10)
                return real_graph.invoke(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(process_one, factory, PausingGraph(), settings)
            try:
                assert started.wait(10)
                assert not process_one(factory, graph2, settings)
            finally:
                release_lock.set()
            assert future.result(timeout=15)
        assert process_one(factory, graph2, settings)
        with factory() as session:
            assert session.get(Release, "8100.1").project_id == 81
            assert session.get(InboxEvent, item.id).status == "completed"
        state = graph2.get_state({"configurable": {"thread_id": item.thread_id}})
        assert state.values["kind"] == "approval"
        assert state.values["workflow_status"] == "approval_recorded"

    failing = {"kind": "tag", "payload": {"project_id": 82, "ref": "refs/tags/REL/8200.1"}}
    event, _ = enqueue(factory, "gitlab", "pg-tag-2", failing)
    class FailAfterCheckpoint:
        def __init__(self, graph):
            self.graph = graph

        def invoke(self, *args, **kwargs):
            self.graph.invoke(*args, **kwargs)
            raise RuntimeError("simulated failure after checkpoint")

    with PostgresSaver.from_conn_string(conn_string) as saver:
        assert process_one(factory, FailAfterCheckpoint(build_graph(saver)), settings)
    with factory() as session:
        assert session.get(InboxEvent, event.id).status == "failed"
        assert session.get(Release, "8200.1") is None
    retry_event(factory, event.id)
    with PostgresSaver.from_conn_string(conn_string) as saver:
        resumed = build_graph(saver)
        assert resumed.get_state({"configurable": {"thread_id": event.thread_id}}).values["last_event_id"] == event.id
        assert process_one(factory, resumed, settings)
    with factory() as session:
        assert session.get(InboxEvent, event.id).status == "completed"
        assert session.get(Release, "8200.1").project_id == 82
        assert len(session.scalars(select(Release).where(Release.version == "8200.1")).all()) == 1


def test_postgres_pollers_serialize_per_project(postgres):
    url, engine, factory = postgres
    settings = Settings(database_url=url, gitlab_url="https://gitlab.example", gitlab_poll_project="poll/test")
    started, release_lock = Event(), Event()

    class GitLab:
        def __init__(self):
            self.updated_at = "2026-01-01T00:00:00Z"
            self.pause = False

        def project_id(self, project):
            if self.pause:
                started.set()
                assert release_lock.wait(10)
            return 500

        def merge_requests(self, project):
            return [{"iid": 1, "target_branch": "master", "updated_at": self.updated_at}]

        def release_tags(self, project):
            return []

    client = GitLab()
    assert poll_once(factory, settings, client) == 0
    client.updated_at = "2026-01-01T00:01:00Z"
    client.pause = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(poll_once, factory, settings, client)
        try:
            assert started.wait(10)
            assert poll_once(factory, settings, client) is None
        finally:
            release_lock.set()
        assert future.result(timeout=15) == 1
    with factory() as session:
        events = session.scalars(select(InboxEvent).where(InboxEvent.source == "gitlab_poll")).all()
        assert len(events) == 1
