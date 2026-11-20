import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from app.llm.advisory import generate_advisory
from app.llm.models import AdvisoryInput, FindingCategory
from app.llm.providers import create_provider
from app.storage.db import create_database
from app.storage.inbox import enqueue
from app.storage.migrate import migrate
from app.storage.models import InboxEvent
from app.supervisor.graph import build_graph
from app.worker import process_one
from config.settings import Settings


def use_mock_http(monkeypatch, response_content, captured, status_code=200):
    original_client = httpx.Client

    def request_handler(request):
        captured.append(request)
        if status_code != 200:
            return httpx.Response(status_code, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": response_content}}],
        })

    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(request_handler), **kwargs)

    monkeypatch.setattr("app.llm.providers.httpx.Client", MockClient)


def test_azure_advisory_sends_only_allowlisted_categories_and_retains_policy(tmp_path, monkeypatch):
    captured = []
    use_mock_http(monkeypatch, '{"summary":"Review the finding.","recommendations":["Consult the runbook."]}', captured)

    class GitLab:
        def __init__(self, *args):
            pass

        def changes(self, project_id, mr_iid):
            return [{"new_path": "deployment/qa2/private-tenant-secret/values.yaml",
                     "diff": "+image:\n+  tag: private-image-secret\n"}]

        def close(self):
            pass

    monkeypatch.setattr("app.worker.GitLabClient", GitLab)
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(
        database_url=url, llm_enabled=True, llm_azure_endpoint="https://example.openai.azure.com",
        llm_azure_auth="api_key", llm_azure_api_key="test-key", llm_model="test-deployment",
    )
    event, _ = enqueue(factory, "gitlab", "private-delivery", {
        "kind": "merge_request",
        "payload": {"project": {"id": 123}, "object_attributes": {"iid": 42, "target_branch": "master"}},
    })
    assert process_one(factory, build_graph(InMemorySaver()), settings)
    with factory() as session:
        stored = session.get(InboxEvent, event.id)
        assert stored.status == "completed"
        assert len(stored.result["violations"]) == 2
        assert stored.result["advisory"]["status"] == "generated"
        assert stored.result["advisory"]["content"]["summary"] == "Review the finding."
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://example.openai.azure.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert json.loads(body["messages"][1]["content"]) == {
        "task": "routing", "categories": ["image_tag_in_deployment", "release_target_mismatch"],
    }
    assert "private" not in request.content.decode()
    assert "deployment/qa2" not in request.content.decode()
    assert "test-key" not in request.content.decode()
    assert "master" not in request.content.decode()
    engine.dispose()


def test_self_hosted_adapter_and_input_validation(monkeypatch):
    captured = []
    use_mock_http(monkeypatch, '{"summary":"Check the status.","recommendations":["Review health."]}', captured)
    settings = Settings(
        llm_provider="self_hosted", llm_model="local-model",
        llm_self_hosted_base_url="http://127.0.0.1:8000/v1",
        llm_self_hosted_api_key="local-token", llm_allow_insecure_loopback=True,
    )
    advisory = AdvisoryInput(task="reconciliation", categories=(FindingCategory.RECONCILIATION_UNHEALTHY,))
    result = generate_advisory(settings, advisory)
    assert result["status"] == "generated"
    assert str(captured[0].url) == "http://127.0.0.1:8000/v1/chat/completions"
    assert json.loads(json.loads(captured[0].content)["messages"][1]["content"]) == {
        "task": "reconciliation", "categories": ["reconciliation_unhealthy"],
    }
    with pytest.raises(ValidationError):
        AdvisoryInput(task="routing", categories=("private-file.yaml",))
    with pytest.raises(ValidationError):
        AdvisoryInput(task="routing", categories=(FindingCategory.RECONCILIATION_HEALTHY,))
    settings.llm_self_hosted_base_url = "http://remote.example/v1"
    assert generate_advisory(settings, advisory)["status"] == "failed"
    assert len(captured) == 1


@pytest.mark.parametrize("auth", ["managed_identity", "workload_identity"])
@pytest.mark.parametrize(
    ("endpoint", "expected_scope"),
    [
        ("https://example.openai.azure.com", "https://cognitiveservices.azure.com/.default"),
        ("https://example.services.ai.azure.com", "https://ai.azure.com/.default"),
    ],
)
def test_azure_identity_provider_is_explicit(auth, endpoint, expected_scope, monkeypatch):
    calls = []

    class Credential:
        def __init__(self, **kwargs):
            calls.append(("credential", kwargs))

        def close(self):
            calls.append(("closed",))

    monkeypatch.setattr("app.llm.providers.ManagedIdentityCredential", Credential)
    monkeypatch.setattr("app.llm.providers.WorkloadIdentityCredential", Credential)

    def token_provider(credential, scope):
        calls.append(("scope", scope))
        return lambda: "test-token"

    monkeypatch.setattr("app.llm.providers.get_bearer_token_provider", token_provider)
    settings = Settings(llm_azure_endpoint=endpoint, llm_model="deployment",
                        llm_azure_auth=auth, llm_azure_client_id="client-id")
    provider = create_provider(settings)
    provider.close()
    assert ("scope", expected_scope) in calls
    assert calls[-1] == ("closed",)
    if auth == "managed_identity":
        assert calls[0] == ("credential", {"client_id": "client-id"})
    else:
        assert calls[0] == ("credential", {})


def test_azure_managed_identity_token_is_used_on_request(monkeypatch):
    captured = []
    use_mock_http(monkeypatch, '{"summary":"Review.","recommendations":["Inspect findings."]}', captured)

    class Credential:
        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr("app.llm.providers.ManagedIdentityCredential", Credential)
    monkeypatch.setattr("app.llm.providers.get_bearer_token_provider", lambda credential, scope: lambda: "entra-token")
    settings = Settings(llm_azure_endpoint="https://example.openai.azure.com", llm_model="deployment")
    advisory = AdvisoryInput(task="routing", categories=(FindingCategory.IMAGE_TAG,))
    assert generate_advisory(settings, advisory)["status"] == "generated"
    assert captured[0].headers["authorization"] == "Bearer entra-token"


def test_model_api_error_is_recorded_without_retry(monkeypatch):
    captured = []
    use_mock_http(monkeypatch, "", captured, status_code=503)
    settings = Settings(llm_azure_endpoint="https://example.openai.azure.com", llm_model="deployment",
                        llm_azure_auth="api_key", llm_azure_api_key="test-key")
    advisory = AdvisoryInput(task="routing", categories=(FindingCategory.IMAGE_TAG,))
    result = generate_advisory(settings, advisory)
    assert result["status"] == "failed"
    assert len(captured) == 1
    assert "test-key" not in json.dumps(result)


def test_model_failures_are_explicit_and_never_erase_reconciliation(tmp_path, monkeypatch):
    captured = []
    use_mock_http(monkeypatch, '{"summary":"", "recommendations":[]}', captured)

    class GitLab:
        def __init__(self, *args):
            pass

        def artifact_file(self, project, job, path):
            return b"application,chart_version,repository\nprivate-app,1.0,private-repo\n"

        def close(self):
            pass

    class Argo:
        def __init__(self, *args):
            pass

        def application(self, name):
            return {"status": {"health": {"status": "Degraded"}}}

        def close(self):
            pass

    monkeypatch.setattr("app.worker.GitLabClient", GitLab)
    monkeypatch.setattr("app.worker.ArgoCDClient", Argo)
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(
        database_url=url, llm_enabled=True, llm_azure_auth="api_key", llm_azure_api_key="test-key",
        llm_azure_endpoint="https://example.openai.azure.com", llm_model="deployment",
    )
    event, _ = enqueue(factory, "gitlab", "job", {"kind": "job", "payload": {
        "project_id": 1, "build_id": 2, "build_name": "deploy_release_secret", "build_status": "success",
    }})
    assert process_one(factory, build_graph(InMemorySaver()), settings)
    with factory() as session:
        stored = session.get(InboxEvent, event.id)
        assert stored.status == "completed"
        assert stored.result["deployments"][0]["healthy"] is False
        assert stored.result["advisory"]["status"] == "failed"
    body = json.loads(captured[0].content)
    assert json.loads(body["messages"][1]["content"]) == {
        "task": "reconciliation", "categories": ["reconciliation_unhealthy"],
    }
    assert "private" not in captured[0].content.decode()
    assert "deploy_release_secret" not in captured[0].content.decode()
    engine.dispose()


def test_azure_endpoint_rejects_non_azure_host():
    settings = Settings(llm_azure_endpoint="https://example.com", llm_model="deployment",
                        llm_azure_auth="api_key", llm_azure_api_key="test-key")
    advisory = AdvisoryInput(task="routing", categories=(FindingCategory.IMAGE_TAG,))
    assert generate_advisory(settings, advisory)["status"] == "failed"
