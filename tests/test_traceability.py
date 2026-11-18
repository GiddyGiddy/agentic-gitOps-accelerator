import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.agents.query_agent import QueryError, QueryRequest, execute_query
from app.gitlab_poller import poll_build_jobs_once, poll_once
from app.llm.models import QueryIntentOutput
from app.main import app, settings as api_settings
from app.storage.db import create_database
from app.storage.inbox import enqueue
from app.storage.migrate import migrate
from app.storage.models import InboxEvent, TraceBuild, TraceDeployment, TraceFix, TraceTag
from app.supervisor.graph import build_graph
from app.tools.gitlab_client import GitLabClient
from app.tools.harbor_client import HarborClient
from app.traceability import index_build
from app.worker import process_one
from config.settings import Settings


BUILD_SHA = "a" * 40
FIX_SHA = "b" * 40
DIGEST = "sha256:" + "c" * 64


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'trace.db'}"
    migrate(url)
    engine, factory = create_database(url)
    yield factory
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(
        gitlab_url="https://gitlab.example", gitlab_token="token", gitlab_poll_project="team/chart",
        harbor_url="https://harbor.example", harbor_username="user", harbor_password="password",
        traceability_enabled=True, traceability_build_jobs="publish_chart",
        traceability_deploy_jobs="deploy_release_emea", traceability_jira_prefixes="GVH",
        traceability_harbor_project="charts", traceability_harbor_repository="repo/chart",
        query_enabled=True, query_secret="query-only", query_applications="viseca-prod",
        query_projects="team/chart", argocd_url="https://argo.example", argocd_token="argo-token",
    )


@pytest.fixture
def upstream(monkeypatch):
    data = {
        "job": {"id": 42, "status": "success", "name": "publish_chart",
                "ref": "release/VN1", "commit": {"id": BUILD_SHA},
                "pipeline": {"id": 100, "sha": BUILD_SHA}},
        "manifest": {
            "schema_version": 1, "commit_sha": BUILD_SHA,
            "chart": {"name": "app", "version": "260801.5", "harbor_project": "charts",
                      "harbor_repository": "repo/chart", "tag": "260801.5", "digest": DIGEST},
            "fixes": [{"commit_sha": FIX_SHA, "patch_paths": ["chart-patch-files/GVH1797/patch.yaml"]}],
        },
        "digest": DIGEST, "ancestor": FIX_SHA, "calls": [], "tags": [],
    }

    class GitLab:
        def __init__(self, *args):
            pass

        def project_id(self, project):
            assert project == "team/chart"
            return 12

        def job(self, project, job):
            assert (project, job) == (12, 42)
            return data["job"]

        def artifact_file(self, project, job, path, *, max_bytes):
            assert (project, job, path, max_bytes) == (12, 42, "gitops-provenance.json", 65536)
            return json.dumps(data["manifest"]).encode()

        def merge_base(self, project, first, second):
            assert (project, first, second) == (12, FIX_SHA, BUILD_SHA)
            return data["ancestor"]

        def commit(self, project, revision):
            return {"id": revision, "message": "GVH1797 - Automated Chart Patch"}

        def repository_file(self, project, path, ref):
            data["calls"].append((project, path, ref))
            return b"patch content is not sent to the model"

        def release_tags(self, project):
            return [tag for tag in data["tags"] if tag["name"].startswith("REL/")]

        def hotfix_tags(self, project):
            return [tag for tag in data["tags"] if tag["name"].startswith("HOT/")]

        def close(self):
            pass

    class Harbor:
        def __init__(self, *args):
            pass

        def artifact(self, project, repository, tag):
            assert (project, repository, tag) == ("charts", "repo/chart", "260801.5")
            return {"digest": data["digest"]}

        def close(self):
            pass

    monkeypatch.setattr("app.traceability.GitLabClient", GitLab)
    monkeypatch.setattr("app.traceability.HarborClient", Harbor)
    monkeypatch.setattr("app.agents.query_agent.GitLabClient", GitLab)
    return data


def use_model(monkeypatch, intent):
    class Provider:
        def classify_query(self, keywords):
            return QueryIntentOutput(intent=intent)

        def close(self):
            pass

    monkeypatch.setattr("app.agents.query_agent.create_provider", lambda settings: Provider())


def test_index_validates_and_deduplicates_evidence(database, settings, upstream):
    with database.begin() as session:
        first = index_build(session, settings, 12, 42)
        assert first.chart_version == "260801.5"
        assert first.digest == DIGEST
        assert index_build(session, settings, 12, 42).id == first.id
    with database() as session:
        assert len(session.scalars(select(TraceBuild)).all()) == 1
        fixes = session.scalars(select(TraceFix)).all()
        assert {(fix.jira_id, fix.commit_sha, fix.evidence_path) for fix in fixes} == {
            ("GVH1797", FIX_SHA, ""), ("GVH1797", FIX_SHA, "chart-patch-files/GVH1797/patch.yaml"),
        }
    assert upstream["calls"][0] == (12, "chart-patch-files/GVH1797/patch.yaml", BUILD_SHA)


@pytest.mark.parametrize("failure", ["different_commit", "not_ancestor", "moved_tag", "unauthorized_registry",
                                      "unsafe_path", "unknown_field", "wrong_job", "digest_as_tag", "too_many_patches"])
def test_invalid_or_conflicting_provenance_never_indexes(database, settings, upstream, failure):
    if failure == "different_commit":
        upstream["manifest"]["commit_sha"] = "d" * 40
    elif failure == "not_ancestor":
        upstream["ancestor"] = "d" * 40
    elif failure == "moved_tag":
        upstream["digest"] = "sha256:" + "d" * 64
    elif failure == "unauthorized_registry":
        upstream["manifest"]["chart"]["harbor_repository"] = "other"
    elif failure == "unsafe_path":
        upstream["manifest"]["fixes"][0]["patch_paths"] = ["chart-patch-files/../secrets"]
    elif failure == "unknown_field":
        upstream["manifest"]["shell_command"] = "unsafe"
    elif failure == "digest_as_tag":
        upstream["manifest"]["chart"]["tag"] = DIGEST
    elif failure == "too_many_patches":
        upstream["manifest"]["fixes"] = [
            {"commit_sha": f"{number:040x}",
             "patch_paths": [f"chart-patch-files/GVH1797/{index}.yaml" for index in range(20)]}
            for number in range(3)
        ]
    else:
        upstream["job"]["name"] = "different"
    with pytest.raises(ValueError):
        with database.begin() as session:
            index_build(session, settings, 12, 42)
    with database() as session:
        assert session.scalars(select(TraceBuild)).all() == []
        assert session.scalars(select(TraceFix)).all() == []


def test_conflicting_second_manifest_preserves_original_record(database, settings, upstream):
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    upstream["manifest"]["chart"]["name"] = "other"
    with pytest.raises(ValueError, match="conflicts"):
        with database.begin() as session:
            index_build(session, settings, 12, 42)
    with database() as session:
        assert session.scalar(select(TraceBuild)).chart_name == "app"


def test_jira_query_returns_authorized_candidates_and_no_false_absence(database, settings, upstream, monkeypatch):
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    use_model(monkeypatch, "jira_fix")
    response = execute_query(settings, QueryRequest(question="Which chart package contained Jira fix GVH1797?"), database)
    assert response.result["jira_id"] == "GVH1797"
    assert len(response.result["candidates"]) == 2
    assert response.result["candidates"][0]["build"]["digest"] == DIGEST
    assert any("/-/commit/" in link for link in response.evidence)
    assert any("/-/jobs/42" in link for link in response.evidence)
    missing = execute_query(settings, QueryRequest(question="Which package contained fix GVH9999?"), database)
    assert missing.result["candidates"] == []
    assert "missing results do not prove absence" in missing.limitations[1]
    with pytest.raises(QueryError) as error:
        execute_query(settings, QueryRequest(question="Which package contained fix GVH1797?", project="team/other"), database)
    assert error.value.status_code == 403
    with pytest.raises(QueryError):
        execute_query(settings, QueryRequest(question="Which package contained fix GVH1797 and GVH1798?"), database)


def test_origin_reports_conflict_without_claiming_deployment_provenance(database, settings, upstream, monkeypatch):
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    upstream["tags"] = [{"name": "HOT/260801.5", "commit": {"id": BUILD_SHA}}]
    use_model(monkeypatch, "release_origin")
    response = execute_query(settings, QueryRequest(
        question="Was this chart a planned release or an emergency hotfix?", chart_version="260801.5",
    ), database)
    assert response.result["candidates"][0]["origin"] == "ambiguous"
    assert response.result["deployment_origin"] == "unverified"
    upstream["tags"][0]["commit"]["id"] = "d" * 40
    moved = execute_query(settings, QueryRequest(question="Was this chart a planned release?",
                                                chart_version="260801.5"), database)
    assert moved.result["candidates"][0]["origin"] == "planned_release"
    assert moved.result["candidates"][0]["matching_tags"] == []


def test_origin_resolves_application_locally_and_never_uses_unauthorized_application(
    database, settings, upstream, monkeypatch
):
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    use_model(monkeypatch, "release_origin")

    class Argo:
        def __init__(self, *args):
            pass

        def application(self, name):
            stamp = datetime.now(timezone.utc).isoformat()
            source = {"repoURL": "https://charts.example", "chart": "app"}
            return {"spec": {"source": source}, "status": {
                "reconciledAt": stamp, "sync": {"status": "Synced", "revision": "260801.5"},
                "health": {"status": "Healthy"},
                "history": [{"source": source, "revision": "260801.5", "deployedAt": stamp}],
            }}

        def close(self):
            pass

    monkeypatch.setattr("app.agents.query_agent.ArgoCDClient", Argo)
    result = execute_query(settings, QueryRequest(
        question="Was viseca-prod a planned release or emergency hotfix?",
    ), database).result
    assert result["application"] == "viseca-prod"
    assert result["chart_version"] == "260801.5"
    assert result["current_chart_observed"] is True
    assert result["deployment_origin"] == "unverified"
    with pytest.raises(QueryError) as error:
        execute_query(settings, QueryRequest(question="Was this a planned release?",
                                             application="other", chart_version="260801.5"), database)
    assert error.value.status_code == 403


def test_authenticated_query_endpoint_reads_index(database, settings, upstream, monkeypatch):
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    use_model(monkeypatch, "jira_fix")
    for field in (
        "query_enabled", "query_secret", "query_applications", "query_projects", "gitlab_url",
        "gitlab_token", "gitlab_poll_project", "harbor_url", "harbor_username", "harbor_password",
        "traceability_enabled", "traceability_build_jobs", "traceability_deploy_jobs",
        "traceability_jira_prefixes", "traceability_harbor_project", "traceability_harbor_repository",
    ):
        monkeypatch.setattr(api_settings, field, getattr(settings, field))
    monkeypatch.setattr(app.state, "sessions", database, raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    payload = {"question": "Which chart contains fix GVH1797?"}
    assert client.post("/queries", json=payload).status_code == 401
    response = client.post("/queries", json=payload, headers={"X-Query-Secret": "query-only"})
    assert response.status_code == 200
    assert response.json()["result"]["candidates"][0]["build"]["digest"] == DIGEST
    assert any("harbor.example" in link for link in response.json()["evidence"])


def test_model_never_receives_jira_identifier_or_repository(monkeypatch, settings):
    from app.agents.query_agent import _model_intent
    captured = []
    original = httpx.Client

    class MockClient(original):
        def __init__(self, **kwargs):
            def handle(request):
                captured.append(request)
                return httpx.Response(200, json={
                    "id": "test", "object": "chat.completion", "created": 1, "model": "test",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": '{"intent":"jira_fix"}'}}],
                })
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("app.llm.providers.httpx.Client", MockClient)
    settings.llm_azure_endpoint = "https://example.openai.azure.com"
    settings.llm_azure_auth = "api_key"
    settings.llm_azure_api_key = "api-secret"
    settings.llm_model = "deployment"
    assert _model_intent(settings, "Which chart in team/chart contains Jira fix GVH1797?") == "jira_fix"
    body = captured[0].content.decode()
    assert all(value not in body for value in ("GVH1797", "team/chart", "api-secret"))


def test_job_and_hotfix_poll_baselines_and_processing(database, settings, upstream):
    class Poller:
        def __init__(self):
            self.jobs = [{"id": 40, "name": "publish_chart", "status": "success"}]
            self.tags = []

        def project_id(self, project):
            return 12

        def recent_successful_jobs(self, project):
            return self.jobs

        def merge_requests(self, project):
            return []

        def release_tags(self, project):
            return []

        def hotfix_tags(self, project):
            return self.tags

    poller = Poller()
    assert poll_build_jobs_once(database, settings, poller) == 0
    assert poll_once(database, settings, poller) == 0
    poller.jobs.append({"id": 42, "name": "publish_chart", "status": "success"})
    poller.jobs.append({"id": 43, "name": "deploy_release_emea", "status": "success"})
    poller.tags.append({"name": "HOT/260801.5", "commit": {"id": BUILD_SHA}})
    assert poll_build_jobs_once(database, settings, poller) == 2
    assert poll_once(database, settings, poller) == 1
    with database() as session:
        events = session.scalars(select(InboxEvent).order_by(InboxEvent.id)).all()
        assert [event.kind for event in events] == ["build", "job", "tag"]
        assert events[1].payload["build_name"] == "deploy_release_emea"
    graph = build_graph(InMemorySaver())
    assert process_one(database, graph, settings)
    with database() as session:
        assert session.scalar(select(TraceBuild)).digest == DIGEST
        job_event = session.scalar(select(InboxEvent).where(InboxEvent.kind == "job"))
        job_event.status = "rejected"
        session.commit()
    assert process_one(database, graph, settings)
    with database() as session:
        assert session.scalar(select(TraceTag)).name == "HOT/260801.5"


def test_poll_failure_does_not_advance_job_baseline(database, settings):
    class Poller:
        jobs = []

        def project_id(self, project):
            return 12

        def recent_successful_jobs(self, project):
            return self.jobs

    poller = Poller()
    assert poll_build_jobs_once(database, settings, poller) == 0
    poller.jobs = [{"id": 42, "name": "publish_chart", "status": "failed"}]
    with pytest.raises(ValueError):
        poll_build_jobs_once(database, settings, poller)
    poller.jobs[0]["status"] = "success"
    assert poll_build_jobs_once(database, settings, poller) == 1


def test_deployment_event_persists_csv_claim_and_argo_observation(database, settings, upstream, monkeypatch):
    class GitLab:
        def __init__(self, *args):
            pass

        def artifact_file(self, project, job, path):
            return b"application,chart_version,repository\nviseca-prod,260801.5,repo/chart\n"

        def close(self):
            pass

    class Argo:
        def __init__(self, *args):
            pass

        def application(self, name):
            return {"spec": {"source": {"targetRevision": "main"}}, "status": {
                "sync": {"status": "OutOfSync", "revision": BUILD_SHA}, "health": {"status": "Degraded"},
            }}

        def close(self):
            pass

    monkeypatch.setattr("app.worker.GitLabClient", GitLab)
    monkeypatch.setattr("app.worker.ArgoCDClient", Argo)
    monkeypatch.setattr("app.agents.query_agent.ArgoCDClient", Argo)
    event, _ = enqueue(database, "test", "deployment", {"kind": "job", "payload": {
        "project_id": 12, "build_id": 43, "build_name": "deploy_release_emea", "build_status": "success",
    }})
    assert process_one(database, build_graph(InMemorySaver()), settings)
    with database() as session:
        assert session.get(InboxEvent, event.id).status == "completed"
        observation = session.scalar(select(TraceDeployment))
        assert (observation.application, observation.chart_version, observation.sync_status,
                observation.health_status, observation.comparison_revision) == (
                    "viseca-prod", "260801.5", "OutOfSync", "Degraded", BUILD_SHA)
    use_model(monkeypatch, "release_origin")
    response = execute_query(settings, QueryRequest(
        question="Was this deployment a planned release?", application="viseca-prod", chart_version="260801.5",
    ), database)
    assert response.result["deployment_observations"][0]["comparison_revision"] == BUILD_SHA
    assert response.result["current_chart_observed"] is False
    assert response.result["deployment_origin"] == "unverified"
    assert any("/-/jobs/43/artifacts/file/deploy_output.csv" in link for link in response.evidence)


def test_gitlab_build_api_uses_bounded_page_and_merge_base():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": FIX_SHA})

    client = GitLabClient("https://gitlab.example", "token")
    client.client.close()
    client.client = httpx.Client(base_url="https://gitlab.example/api/v4/",
                                 transport=httpx.MockTransport(handle))
    try:
        assert client.recent_successful_jobs("team/chart") == []
        assert client.merge_base("team/chart", FIX_SHA, BUILD_SHA) == FIX_SHA
        assert calls[0].url.params["per_page"] == "100"
        assert calls[0].url.params["scope[]"] == "success"
        assert calls[1].url.params.get_list("refs[]") == [FIX_SHA, BUILD_SHA]
    finally:
        client.close()


def test_harbor_nested_repository_is_double_encoded():
    client = HarborClient("https://harbor.example", "user", "password")
    client.client.close()
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"digest": DIGEST})

    client.client = httpx.Client(base_url="https://harbor.example/api/v2.0/",
                                 transport=httpx.MockTransport(handle))
    try:
        assert client.artifact("charts", "repo/chart", "260801.5")["digest"] == DIGEST
        assert b"/repositories/repo%252Fchart/artifacts/260801.5" in seen[0].url.raw_path
    finally:
        client.close()


def test_explicit_backfill_is_durable_and_idempotent(database, settings, upstream, monkeypatch):
    from app import traceability
    settings.database_url = str(database.kw["bind"].url)
    monkeypatch.setattr(traceability, "get_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["traceability", "--job-id", "42", "--job-id", "42"])
    traceability.main()
    traceability.main()
    with database() as session:
        events = session.scalars(select(InboxEvent).where(InboxEvent.source == "trace_backfill")).all()
        assert len(events) == 1
        assert events[0].payload == {"project_id": 12, "job_id": 42}
