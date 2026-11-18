import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.gitlab_poller import poll_once
from app.storage.db import create_database
from app.storage.migrate import migrate
from app.storage.models import GitLabPollState, InboxEvent, Release
from app.supervisor.graph import build_graph
from app.tools.gitlab_client import GitLabClient
from app.worker import process_one
from config.settings import Settings, get_settings


class FakeGitLab:
    def __init__(self):
        self.mrs = [{"iid": 1, "target_branch": "master", "updated_at": "2026-01-01T00:00:00Z"}]
        self.tags = [{"name": "REL/old", "commit": {"id": "old-sha"}}]

    def project_id(self, project):
        return 12

    def merge_requests(self, project):
        return self.mrs

    def release_tags(self, project):
        return self.tags


def test_mounted_token_takes_precedence_and_missing_file_fails(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("GITOPS_GITLAB_TOKEN", "old-token")
    monkeypatch.setenv("GITOPS_GITLAB_TOKEN_FILE", str(token_file))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.gitlab_token == "file-token"
        assert "file-token" not in repr(settings)
        token_file.unlink()
        get_settings.cache_clear()
        with pytest.raises(FileNotFoundError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_baseline_changes_and_restart_are_durable(tmp_path):
    url = f"sqlite:///{tmp_path / 'poll.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, gitlab_url="https://gitlab.example", gitlab_poll_project="group/project")
    client = FakeGitLab()

    assert poll_once(factory, settings, client) == 0
    with factory() as session:
        assert session.scalars(select(InboxEvent)).all() == []
    client.mrs[0]["updated_at"] = "2026-01-01T01:00:00+01:00"
    assert poll_once(factory, settings, client) == 0
    client.mrs[0]["updated_at"] = "2026-01-01T00:01:00Z"
    client.tags.append({"name": "REL/new", "commit": {"id": "new-sha"}})
    assert poll_once(factory, settings, client) == 2
    engine.dispose()

    engine, factory = create_database(url)
    assert poll_once(factory, settings, client) == 0
    with factory() as session:
        events = session.scalars(select(InboxEvent).order_by(InboxEvent.id)).all()
        assert [event.kind for event in events] == ["merge_request", "tag"]
        assert events[0].payload["object_attributes"]["iid"] == 1
        assert events[0].payload["project"]["id"] == 12
        assert events[1].payload == {
            "project_id": 12, "ref": "refs/tags/REL/new", "checkout_sha": "new-sha"
        }
    engine.dispose()


def test_invalid_observation_rolls_back_events_and_baseline(tmp_path):
    url = f"sqlite:///{tmp_path / 'poll.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, gitlab_url="https://gitlab.example", gitlab_poll_project="group/project")
    client = FakeGitLab()
    poll_once(factory, settings, client)
    client.mrs[0]["updated_at"] = "2026-01-01T00:01:00Z"
    client.tags.append({"name": "REL/bad/name", "commit": {"id": "sha"}})
    with pytest.raises(ValueError):
        poll_once(factory, settings, client)
    with factory() as session:
        assert session.scalars(select(InboxEvent)).all() == []
        assert session.scalar(select(GitLabPollState)).generation == 0
    client.tags[-1]["name"] = "REL/good"
    assert poll_once(factory, settings, client) == 2
    engine.dispose()


def test_observed_tag_recreation_and_changed_commit_are_enqueued(tmp_path):
    url = f"sqlite:///{tmp_path / 'poll.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, gitlab_url="https://gitlab.example", gitlab_poll_project="group/project")
    client = FakeGitLab()
    poll_once(factory, settings, client)
    client.tags.clear()
    assert poll_once(factory, settings, client) == 0
    client.tags.append({"name": "REL/old", "commit": {"id": "old-sha"}})
    assert poll_once(factory, settings, client) == 1
    client.tags[0]["commit"]["id"] = "new-sha"
    assert poll_once(factory, settings, client) == 1
    engine.dispose()


def test_poll_observations_are_processed_by_worker(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'poll.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, gitlab_url="https://gitlab.example", gitlab_poll_project="group/project")
    client = FakeGitLab()
    poll_once(factory, settings, client)
    client.mrs[0]["updated_at"] = "2026-01-01T00:01:00Z"
    client.tags.append({"name": "REL/1.0", "commit": {"id": "new-sha"}})
    assert poll_once(factory, settings, client) == 2

    class WorkerGitLab:
        def __init__(self, *args):
            pass

        def changes(self, project_id, iid):
            return []

        def close(self):
            pass

    monkeypatch.setattr("app.worker.GitLabClient", WorkerGitLab)
    graph = build_graph(InMemorySaver())
    assert process_one(factory, graph, settings)
    assert process_one(factory, graph, settings)
    with factory() as session:
        events = session.scalars(select(InboxEvent).order_by(InboxEvent.id)).all()
        assert [event.status for event in events] == ["completed", "completed"]
        assert events[0].result["status"] == "evaluated"
        assert session.get(Release, "1.0").project_id == 12
    engine.dispose()


def test_changed_project_identity_does_not_replace_baseline(tmp_path):
    url = f"sqlite:///{tmp_path / 'poll.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, gitlab_url="https://gitlab.example", gitlab_poll_project="group/project")
    client = FakeGitLab()
    poll_once(factory, settings, client)
    client.project_id = lambda project: 13
    with pytest.raises(ValueError, match="project ID changed"):
        poll_once(factory, settings, client)
    with factory() as session:
        assert session.scalar(select(GitLabPollState)).project_id == 12
        assert session.scalars(select(InboxEvent)).all() == []
    engine.dispose()


def test_gitlab_pagination_continues_after_a_short_page_and_surfaces_errors():
    calls = []

    def handler(request):
        calls.append((request.url.path, request.url.params["page"]))
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json=[{"iid": 1}])
        if page == 2:
            return httpx.Response(200, json=[{"iid": 2}])
        return httpx.Response(200, json=[])

    client = GitLabClient("https://gitlab.example", "token")
    client.client.close()
    client.client = httpx.Client(base_url="https://gitlab.example/api/v4/", transport=httpx.MockTransport(handler))
    try:
        assert [item["iid"] for item in client.merge_requests("group/project")] == [1, 2]
        assert [page for _, page in calls] == ["1", "2", "3"]
    finally:
        client.close()

    client = GitLabClient("https://gitlab.example", "token")
    client.client.close()
    client.client = httpx.Client(
        base_url="https://gitlab.example/api/v4/",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[{"iid": 1}])),
    )
    try:
        with pytest.raises(ValueError, match="repeated page"):
            client.merge_requests("group/project")
    finally:
        client.close()

    client = GitLabClient("https://gitlab.example", "token")
    client.client.close()
    client.client = httpx.Client(
        base_url="https://gitlab.example/api/v4/",
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.release_tags("group/project")
    finally:
        client.close()
