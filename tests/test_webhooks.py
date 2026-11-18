from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.main import app, settings
from app.storage.migrate import migrate
from app.supervisor.graph import build_graph
from app.worker import process_one


def test_gitlab_webhook_can_be_disabled(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(database_url)
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "gitlab-test")
    monkeypatch.setattr(settings, "gitlab_webhook_enabled", False)
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/gitlab",
            headers={"X-Gitlab-Token": "gitlab-test", "X-Gitlab-Event": "Tag Push Hook",
                     "X-Gitlab-Event-UUID": "delivery-1"},
            json={"ref": "refs/tags/REL/1.0", "checkout_sha": "abc", "project_id": 12},
        )
        assert response.status_code == 503


def test_webhooks_are_durable_deduplicated_and_processed(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(database_url)
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "gitlab-test")
    monkeypatch.setattr(settings, "teams_gateway_secret", "gateway-test")
    monkeypatch.setattr(settings, "teams_approvers", "release-manager")
    monkeypatch.setattr(settings, "status_secret", "status-test")
    tag = {"ref": "refs/tags/REL/260901.4", "checkout_sha": "abc", "project_id": 12}
    headers = {"X-Gitlab-Token": "gitlab-test", "X-Gitlab-Event": "Tag Push Hook",
               "X-Gitlab-Event-UUID": "delivery-1"}
    graph = build_graph(InMemorySaver())
    with TestClient(app) as client:
        assert client.post("/webhooks/gitlab", json=tag).status_code == 401
        response = client.post("/webhooks/gitlab", json=tag, headers=headers)
        assert response.status_code == 202
        event_id = response.json()["event_id"]
        assert client.post("/webhooks/gitlab", json=tag, headers=headers).json()["duplicate"] is True
        assert client.post("/webhooks/gitlab", json={**tag, "checkout_sha": "other"}, headers=headers).status_code == 422
        assert client.get(f"/events/{event_id}").status_code == 401
        assert process_one(app.state.sessions, graph, settings)
        status = client.get(f"/events/{event_id}", headers={"X-Status-Secret": "status-test"}).json()
        assert status["status"] == "completed"
        assert status["result"]["release"] == "260901.4"
        approval = {"project_id": 12, "release": "260901.4", "wave": "wave2",
                    "actor": "outsider", "request_id": "approval-1"}
        gateway_headers = {"X-Gateway-Secret": "gateway-test"}
        assert client.post("/chatops/approvals", json=approval, headers=gateway_headers).status_code == 403
        approval["actor"] = "release-manager"
        assert client.post("/chatops/approvals", json=approval, headers=gateway_headers).status_code == 202
        assert process_one(app.state.sessions, graph, settings)
        assert client.post("/chatops/approvals", json=approval, headers=gateway_headers).json()["duplicate"] is True


def test_deploy_job_fetches_artifact_and_checks_argocd(tmp_path, monkeypatch):
    class GitLab:
        def __init__(self, *args):
            pass

        def artifact_file(self, project, job, path):
            assert (project, job, path) == (12, 42, "deploy_output.csv")
            return b"application,chart_version,repository\nvn-base,260901.4,helix-argocd\n"

        def close(self):
            pass

    class Argo:
        def __init__(self, *args):
            pass

        def application(self, name):
            assert name == "vn-base"
            return {
                "spec": {"source": {"targetRevision": "260901.4"}},
                "status": {
                    "sync": {"status": "Synced", "revision": "260901.4"},
                    "health": {"status": "Healthy"},
                },
            }

        def close(self):
            pass

    monkeypatch.setattr("app.worker.GitLabClient", GitLab)
    monkeypatch.setattr("app.worker.ArgoCDClient", Argo)
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(database_url)
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "gitlab-test")
    monkeypatch.setattr(settings, "status_secret", "status-test")
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/gitlab",
            headers={"X-Gitlab-Token": "gitlab-test", "X-Gitlab-Event": "Job Hook",
                     "X-Gitlab-Event-UUID": "job-1"},
            json={"project_id": 12, "build_id": 42, "build_name": "deploy_release_emea_vn_base",
                  "build_status": "success"},
        )
        assert response.status_code == 202
        assert process_one(app.state.sessions, build_graph(InMemorySaver()), settings)
        result = client.get(
            f"/events/{response.json()['event_id']}", headers={"X-Status-Secret": "status-test"}
        ).json()
        assert result["result"]["deployments"][0]["healthy"] is True
