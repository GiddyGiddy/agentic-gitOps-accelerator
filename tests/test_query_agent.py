import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.query_agent import QueryError, QueryRequest, execute_query
from app.llm.models import QueryIntentOutput
from app.llm.providers import create_provider
from app.main import app, settings as api_settings
from app.tools.gitlab_client import GitLabClient
from config.settings import Settings


def configured():
    return Settings(
        query_enabled=True, query_secret="query-only", query_applications="viseca-prod",
        query_projects="team/chart", gitlab_url="https://gitlab.example", gitlab_token="gitlab-token",
        argocd_url="https://argo.example", argocd_token="argo-token",
        llm_azure_endpoint="https://example.openai.azure.com", llm_model="deployment",
        llm_azure_auth="api_key", llm_azure_api_key="model-token",
    )


def model(monkeypatch, intent):
    class Provider:
        def classify_query(self, keywords):
            return QueryIntentOutput(intent=intent)

        def close(self):
            pass

    monkeypatch.setattr("app.agents.query_agent.create_provider", lambda settings: Provider())


def application(source, revision="260801.4", sync="Synced", reconciled_at=None):
    stamp = datetime.now(timezone.utc).isoformat()
    return {
        "spec": {"source": source},
        "status": {
            "reconciledAt": reconciled_at or stamp,
            "sync": {"status": sync, "revision": revision}, "health": {"status": "Healthy"},
            "history": [{"source": source, "revision": revision, "deployedAt": stamp}],
        },
    }


def argo(monkeypatch, data):
    class Client:
        def __init__(self, *args):
            pass

        def application(self, name):
            assert name == "viseca-prod"
            return data

        def close(self):
            pass

    monkeypatch.setattr("app.agents.query_agent.ArgoCDClient", Client)


def test_query_endpoint_authentication_and_fail_closed(monkeypatch):
    monkeypatch.setattr(api_settings, "query_enabled", False)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/queries", json={"question": "which chart version"}).status_code == 503
    monkeypatch.setattr(api_settings, "query_enabled", True)
    monkeypatch.setattr(api_settings, "query_secret", "query-only")
    monkeypatch.setattr(api_settings, "query_applications", "")
    monkeypatch.setattr(api_settings, "query_projects", "")
    assert client.post("/queries", json={"question": "which chart version"}).status_code == 401
    assert client.post("/queries", headers={"X-Query-Secret": "wrong"},
                       json={"question": "which chart version"}).status_code == 401
    assert client.post("/queries", headers={"X-Query-Secret": "query-only"},
                       json={"question": "which chart version"}).status_code == 503


def test_model_sees_only_fixed_generic_keywords(monkeypatch):
    captured = []
    original_client = httpx.Client

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": '{"intent":"branch_comparison"}'}}],
        })

    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("app.llm.providers.httpx.Client", MockClient)
    from app.agents.query_agent import _model_intent
    assert _model_intent(configured(), "What changed between master and release/VN-secret?") == "branch_comparison"
    body = json.loads(captured[0].content)
    assert json.loads(body["messages"][1]["content"]) == {"keywords": ["and", "between", "changed", "what"]}
    assert all(value not in body["messages"][1]["content"] for value in ("master", "release", "secret"))
    assert "model-token" not in captured[0].content.decode()


@pytest.mark.parametrize("output", [
    '{"intent":"arbitrary_tool"}', '{"intent":"deployed_version","url":"https://evil.example"}',
    '{"intent":1}', "not json",
])
def test_invalid_model_output_rejected(monkeypatch, output):
    original_client = httpx.Client

    class MockClient(original_client):
        def __init__(self, **kwargs):
            def handle(request):
                return httpx.Response(200, json={
                    "id": "test", "object": "chat.completion", "created": 1, "model": "test",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": output}}],
                })
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("app.llm.providers.httpx.Client", MockClient)
    provider = create_provider(configured())
    try:
        with pytest.raises(ValueError):
            provider.classify_query(["which", "chart"])
    finally:
        provider.close()


def test_helm_version_requires_matching_fresh_history(monkeypatch):
    model(monkeypatch, "deployed_version")
    source = {"repoURL": "https://charts.example", "chart": "example", "targetRevision": "260801.*"}
    data = application(source)
    argo(monkeypatch, data)
    request = QueryRequest(question="Which chart version is deployed in viseca-prod?")
    result = execute_query(configured(), request)
    assert result.result["chart_version"] == "260801.4"
    assert result.result["reconciliation_fresh"] is True
    assert result.evidence == ["https://argo.example/applications/viseca-prod"]
    data["status"]["sync"]["revision"] = "different"
    assert execute_query(configured(), request).result["chart_version"] is None
    data["status"]["sync"]["revision"] = "260801.4"
    data["status"]["reconciledAt"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    stale = execute_query(configured(), request).result
    assert stale["chart_version"] is None
    assert stale["reconciliation_fresh"] is False
    data["status"]["reconciledAt"] = datetime.now(timezone.utc).isoformat()
    data["status"]["sync"]["status"] = "OutOfSync"
    assert execute_query(configured(), request).result["chart_version"] is None
    data["spec"]["sources"] = [source]
    assert "Multi-source" in execute_query(configured(), request).result["unavailable_reason"]


def test_git_chart_uses_recorded_commit_and_authorized_repository(monkeypatch):
    model(monkeypatch, "deployed_version")
    source = {"repoURL": "https://gitlab.example/team/chart.git", "path": "deploy/chart"}
    revision = "a" * 40
    argo(monkeypatch, application(source, revision))
    calls = []

    class Client:
        def __init__(self, *args):
            pass

        def repository_file(self, project, path, ref):
            calls.append((project, path, ref))
            return b"apiVersion: v2\nversion: 1.10\n"

        def close(self):
            pass

    monkeypatch.setattr("app.agents.query_agent.GitLabClient", Client)
    request = QueryRequest(question="Which chart version is deployed in viseca-prod?")
    result = execute_query(configured(), request)
    assert result.result["chart_version"] == "1.10"
    assert calls == [("team/chart", "deploy/chart/Chart.yaml", revision)]
    assert revision in result.evidence[1]
    source["path"] = "."
    assert execute_query(configured(), request).result["chart_version"] == "1.10"
    assert calls[-1] == ("team/chart", "Chart.yaml", revision)
    source["repoURL"] = "https://gitlab.example/team/other.git"
    assert execute_query(configured(), request).result["chart_version"] is None
    assert len(calls) == 2


def test_comparison_allowlists_and_incomplete_diff(monkeypatch):
    model(monkeypatch, "branch_comparison")

    class Client:
        def __init__(self, *args):
            pass

        def compare(self, project, from_ref, to_ref):
            assert (project, from_ref, to_ref) == ("team/chart", "master", "release/VN1")
            return {"compare_timeout": False, "diffs": [
                {"old_path": "Chart.yaml", "new_path": "Chart.yaml",
                 "diff": "-version: 1.0\n+version: 1.1\n", "collapsed": True},
            ]}

        def close(self):
            pass

    monkeypatch.setattr("app.agents.query_agent.GitLabClient", Client)
    result = execute_query(configured(), QueryRequest(question="What changed between master and release/VN1?"))
    assert result.result["incomplete"] is True
    assert result.result["changes"][0]["version_lines"] == ["-version: 1.0", "+version: 1.1"]
    with pytest.raises(QueryError) as forbidden:
        execute_query(configured(), QueryRequest(question="Compare branches", project="team/other",
                                                 from_ref="master", to_ref="release/VN1"))
    assert forbidden.value.status_code == 403
    with pytest.raises(QueryError):
        execute_query(configured(), QueryRequest(question="Compare branches", from_ref="../private", to_ref="main"))


def test_unauthorized_application_and_reused_secret(monkeypatch):
    model(monkeypatch, "deployed_version")
    with pytest.raises(QueryError) as error:
        execute_query(configured(), QueryRequest(question="Which chart version?", application="other"))
    assert error.value.status_code == 403
    settings = configured()
    settings.status_secret = settings.query_secret
    with pytest.raises(QueryError) as error:
        execute_query(settings, QueryRequest(question="Which chart version?", application="viseca-prod"))
    assert error.value.status_code == 503


def test_upstream_error_is_explicit(monkeypatch):
    model(monkeypatch, "branch_comparison")

    class Client:
        def __init__(self, *args):
            pass

        def compare(self, *args):
            raise httpx.ConnectError("sensitive upstream information")

        def close(self):
            pass

    monkeypatch.setattr("app.agents.query_agent.GitLabClient", Client)
    for name in ("query_enabled", "query_secret", "query_applications", "query_projects",
                 "gitlab_url", "gitlab_token"):
        monkeypatch.setattr(api_settings, name, getattr(configured(), name))
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/queries", headers={"X-Query-Secret": "query-only"},
                           json={"question": "Compare branches", "from_ref": "master", "to_ref": "main"})
    assert response.status_code == 502
    assert "sensitive" not in response.text


def test_gitlab_comparison_endpoint_and_size_limit():
    client = GitLabClient("https://gitlab.example", "token")
    client.client.close()
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"diffs": [], "compare_timeout": False})

    client.client = httpx.Client(base_url="https://gitlab.example/api/v4/",
                                 transport=httpx.MockTransport(handle))
    assert client.compare("team/chart", "master", "release/VN1")["diffs"] == []
    assert seen[0].url.params["from"] == "master"
    assert seen[0].url.params["to"] == "release/VN1"
    assert seen[0].url.params["straight"] == "true"
    client.close()
    client.client = httpx.Client(
        base_url="https://gitlab.example/api/v4/",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 2_000_001)),
    )
    with pytest.raises(ValueError, match="size limit"):
        client.compare("team/chart", "master", "main")
    client.close()


def test_authenticated_query_returns_evidence(monkeypatch):
    model(monkeypatch, "deployed_version")
    argo(monkeypatch, application({"repoURL": "https://charts.example", "chart": "example"}))
    for name in ("query_enabled", "query_secret", "query_applications", "query_projects",
                 "gitlab_url", "gitlab_token", "argocd_url", "argocd_token"):
        monkeypatch.setattr(api_settings, name, getattr(configured(), name))
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/queries", headers={"X-Query-Secret": "query-only"},
                           json={"question": "Which chart version is deployed in viseca-prod?"})
    assert response.status_code == 200
    assert response.json()["result"]["chart_version"] == "260801.4"
    assert response.json()["evidence"] == ["https://argo.example/applications/viseca-prod"]
    assert response.json()["observed_at"]


def test_ambiguous_and_write_requests_are_rejected(monkeypatch):
    model(monkeypatch, "branch_comparison")
    for question in ("Do not compare branches", "Don't compare branches", "rollback to main"):
        with pytest.raises(QueryError) as error:
            execute_query(configured(), QueryRequest(question=question))
        assert error.value.status_code == 422
