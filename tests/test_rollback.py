import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app, settings as api_settings
from app.release_bundles import BundleError
from app.rollback import (
    RollbackRequest, approve_plan, create_plan, execute_plan, preview_plan, read_plan,
)
from app.storage.models import AuditEvent, RollbackPlan
from app.supervisor.guardrails import PolicyViolation
from app.tools.gitlab_client import GitLabClient
from app.traceability import index_build
from test_release_bundles import (
    CONFIG_SHA, OTHER_SHA, approve, database, draft, settings, upstream,
)
from test_postgres_workflow import postgres
from test_query_agent import model


@pytest.fixture
def rollback_setup(settings, upstream, monkeypatch):
    settings.rollback_enabled = True
    settings.rollback_gateway_secret = "rollback-only"
    settings.rollback_targets = json.dumps({
        "viseca-prod": {"branch": "main", "files": ["env/values.yaml", "Chart.yaml"]},
    })
    state = {
        "head": OTHER_SHA,
        "old": {"env/values.yaml": b"image: old\n", "Chart.yaml": b"version: old\n"},
        "current": {"env/values.yaml": b"image: current\n", "Chart.yaml": b"version: old\n"},
        "writes": [], "fail_commit": False,
    }

    class GitLab:
        def __init__(self, base_url, token, write_enabled=False):
            self.write_enabled = write_enabled

        def project_id(self, project):
            return upstream["project_ids"][project]

        def branch(self, project, branch):
            assert (project, branch) == (13, "main")
            return {"name": branch, "commit": {"id": state["head"]}}

        def repository_file(self, project, path, ref):
            assert project == 13
            return state["old" if ref == CONFIG_SHA else "current"][path]

        def create_commit(self, project, branch, message, actions):
            assert self.write_enabled
            state["writes"].append(("commit", project, branch, actions))
            if state["fail_commit"]:
                raise httpx.ReadTimeout("simulated uncertain result")
            return {"id": "d" * 40, "parent_ids": [OTHER_SHA]}

        def create_branch(self, project, branch, ref):
            assert self.write_enabled
            state["writes"].append(("branch", project, branch, ref))
            return {"name": branch, "commit": {"id": ref}}

        def create_merge_request(self, project, source, target, title, *, description):
            assert self.write_enabled
            state["writes"].append(("mr", project, source, target, title, description))
            return {"iid": 9, "source_branch": source, "target_branch": target,
                    "state": "opened", "title": title}

        def close(self):
            pass

    monkeypatch.setattr("app.rollback.GitLabClient", GitLab)
    return state


def plan_for(database, settings):
    bundle = draft(database, settings)
    approve(database, settings, bundle.fingerprint)
    request = RollbackRequest(bundle_fingerprint=bundle.fingerprint, application="viseca-prod",
                              actor="manager", request_id="rollback-1")
    return create_plan(database, settings, request), request


def test_approved_plan_creates_one_draft_mr(database, settings, upstream, rollback_setup):
    plan, request = plan_for(database, settings)
    assert plan.status == "draft"
    assert [file.path for file in plan.plan.files] == ["env/values.yaml"]
    assert not rollback_setup["writes"]
    assert create_plan(database, settings, request).id == plan.id
    diff = preview_plan(database, settings, plan.id, "manager")[0].diff
    assert "-image: current" in diff and "+image: old" in diff
    with pytest.raises(BundleError):
        approve_plan(database, settings, plan.id, "manager", "0" * 64)
    assert approve_plan(database, settings, plan.id, "manager", plan.fingerprint).status == "approved"
    assert approve_plan(database, settings, plan.id, "manager", plan.fingerprint).status == "approved"
    with pytest.raises(BundleError):
        execute_plan(database, settings, plan.id, "manager")
    settings.write_enabled = True
    result = execute_plan(database, settings, plan.id, "manager")
    assert result.status == "completed" and result.merge_request_iid == 9
    assert execute_plan(database, settings, plan.id, "manager").merge_request_iid == 9
    assert len(rollback_setup["writes"]) == 3
    _, project, branch, actions = rollback_setup["writes"][1]
    assert (project, branch) == (13, f"agent-rollback/{plan.id}")
    assert rollback_setup["writes"][0][3] == OTHER_SHA
    assert len(actions) == 1 and actions[0]["file_path"] == "env/values.yaml"
    assert base64.b64decode(actions[0]["content"]) == b"image: old\n"
    assert rollback_setup["writes"][2][4].startswith("Draft:")
    with database() as session:
        kinds = {item.kind for item in session.scalars(select(AuditEvent)).all()}
        assert {"rollback_proposed", "rollback_approved", "rollback_started", "rollback_mr_created"} <= kinds


@pytest.mark.parametrize("problem", ["branch", "scope", "project", "bundle_digest", "plan_tamper",
                                      "approver_revoked"])
def test_changed_evidence_blocks_execution_without_writes(database, settings, upstream, rollback_setup, problem):
    plan, _ = plan_for(database, settings)
    approve_plan(database, settings, plan.id, "reviewer", plan.fingerprint)
    settings.write_enabled = True
    if problem == "branch":
        rollback_setup["head"] = "e" * 40
    elif problem == "scope":
        settings.rollback_targets = json.dumps({"viseca-prod": {"branch": "main", "files": ["Chart.yaml"]}})
    elif problem == "project":
        upstream["project_ids"]["team/config"] = 99
    elif problem == "bundle_digest":
        upstream["chart_digest"] = "sha256:" + "f" * 64
    elif problem == "approver_revoked":
        settings.teams_approvers = "manager"
    else:
        with database.begin() as session:
            row = session.get(RollbackPlan, plan.id)
            row.plan = {**row.plan, "target_branch": "other"}
    with pytest.raises(BundleError):
        execute_plan(database, settings, plan.id, "manager")
    assert not rollback_setup["writes"]


def test_uncertain_write_is_not_retried(database, settings, upstream, rollback_setup):
    plan, _ = plan_for(database, settings)
    approve_plan(database, settings, plan.id, "reviewer", plan.fingerprint)
    settings.write_enabled = True
    rollback_setup["fail_commit"] = True
    with pytest.raises(BundleError) as error:
        execute_plan(database, settings, plan.id, "manager")
    assert error.value.status_code == 502
    assert read_plan(database, settings, plan.id, "manager").status == "uncertain"
    with pytest.raises(BundleError):
        execute_plan(database, settings, plan.id, "manager")
    assert len(rollback_setup["writes"]) == 2


def test_unapproved_bundle_cannot_create_plan(database, settings, upstream, rollback_setup):
    bundle = draft(database, settings)
    with pytest.raises(BundleError) as error:
        create_plan(database, settings, RollbackRequest(
            bundle_fingerprint=bundle.fingerprint, application="viseca-prod", actor="manager", request_id="draft",
        ))
    assert error.value.status_code == 409
    assert not rollback_setup["writes"]


def test_preview_preserves_missing_final_newlines(database, settings, upstream, rollback_setup):
    rollback_setup["old"]["env/values.yaml"] = b"old"
    rollback_setup["current"]["env/values.yaml"] = b"new"
    plan, _ = plan_for(database, settings)
    diff = preview_plan(database, settings, plan.id, "manager")[0].diff
    assert "-new\n\\ No newline at end of file\n+old\n\\ No newline at end of file\n" in diff


def test_gitlab_commit_api_requires_write_opt_in():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"id": "d" * 40, "parent_ids": [OTHER_SHA]})

    client = GitLabClient("https://gitlab.example", "token")
    client.client.close()
    client.client = httpx.Client(base_url="https://gitlab.example/api/v4/",
                                transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PolicyViolation):
            client.create_commit(13, "agent-rollback/abc", "rollback", [])
        assert not seen
        client.write_enabled = True
        client.create_commit(13, "agent-rollback/abc", "rollback", [
            {"action": "update", "file_path": "env/values.yaml", "encoding": "base64", "content": "b2xk"},
        ])
        path, body = seen[0]
        assert path == "/api/v4/projects/13/repository/commits"
        assert body["branch"] == "agent-rollback/abc" and "start_sha" not in body
        assert body["actions"][0]["content"] == "b2xk"
    finally:
        client.close()


def test_postgres_concurrent_execution_claims_once(postgres, settings, upstream, rollback_setup, monkeypatch):
    _, _, factory = postgres
    plan, _ = plan_for(factory, settings)
    approve_plan(factory, settings, plan.id, "reviewer", plan.fingerprint)
    settings.write_enabled = True
    started, release = Event(), Event()
    from app.rollback import GitLabClient
    create_branch = GitLabClient.create_branch

    def pause(self, *args):
        started.set()
        assert release.wait(10)
        return create_branch(self, *args)

    monkeypatch.setattr(GitLabClient, "create_branch", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_plan, factory, settings, plan.id, "manager")
        try:
            assert started.wait(10)
            with pytest.raises(BundleError) as error:
                execute_plan(factory, settings, plan.id, "manager")
            assert error.value.status_code == 409
        finally:
            release.set()
        assert future.result(timeout=15).status == "completed"
    assert len(rollback_setup["writes"]) == 3


def test_postgres_api_query_bundle_drift_and_rollback_flow(postgres, settings, upstream, rollback_setup, monkeypatch):
    url, _, factory = postgres
    settings.database_url = url
    settings.write_enabled = True
    for name in type(settings).model_fields:
        monkeypatch.setattr(api_settings, name, getattr(settings, name))
    model(monkeypatch, "deployed_version")
    from app.release_bundles import ArgoCDClient
    monkeypatch.setattr("app.agents.query_agent.ArgoCDClient", ArgoCDClient)
    with factory.begin() as session:
        index_build(session, settings, 12, 42)

    query_headers = {"X-Query-Secret": "query-only"}
    bundle_headers = {"X-Bundle-Gateway-Secret": "gateway-only"}
    rollback_headers = {"X-Rollback-Gateway-Secret": "rollback-only"}
    with TestClient(app, raise_server_exceptions=False) as client:
        query = {"question": "Which chart version is deployed?", "application": "viseca-prod"}
        assert client.post("/queries", json=query).status_code == 401
        response = client.post("/queries", json=query, headers=query_headers)
        assert response.status_code == 200 and response.json()["result"]["chart_version"] == "260801.5"

        payload = {"project": "team/chart", "job_id": 42, "actor": "manager"}
        assert client.post("/release-bundles", json=payload, headers=query_headers).status_code == 401
        response = client.post("/release-bundles", json=payload, headers=bundle_headers)
        assert response.status_code == 200
        fingerprint = response.json()["fingerprint"]
        response = client.post(f"/release-bundles/{fingerprint}/approval", headers=bundle_headers,
                               json={"actor": "reviewer", "request_id": "approval-1"})
        assert response.status_code == 200 and response.json()["status"] == "approved"
        response = client.get(f"/release-bundles/{fingerprint}/drift/viseca-prod", headers=query_headers)
        assert response.status_code == 200 and response.json()["chart_digest"]["status"] == "unknown"

        payload = {"bundle_fingerprint": fingerprint, "application": "viseca-prod",
                   "actor": "manager", "request_id": uuid4().hex}
        response = client.post("/rollbacks", json=payload, headers=rollback_headers)
        assert response.status_code == 200 and response.json()["status"] == "draft"
        plan = response.json()
        plan_id = plan["id"]
        assert client.post(f"/rollbacks/{plan_id}/execute", json={"actor": "manager"},
                           headers=rollback_headers).status_code == 409
        response = client.get(f"/rollbacks/{plan_id}/preview", params={"actor": "manager"},
                              headers=rollback_headers)
        assert response.status_code == 200 and "+image: old" in response.json()[0]["diff"]
        response = client.post(f"/rollbacks/{plan_id}/approval", headers=rollback_headers,
                               json={"actor": "manager", "fingerprint": plan["fingerprint"]})
        assert response.status_code == 200 and response.json()["status"] == "approved"
        response = client.post(f"/rollbacks/{plan_id}/execute", json={"actor": "manager"},
                               headers=rollback_headers)
        assert response.status_code == 200 and response.json()["merge_request_iid"] == 9

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f"/rollbacks/{plan_id}", params={"actor": "manager"}, headers=rollback_headers)
        assert response.status_code == 200 and response.json()["status"] == "completed"
    assert len(rollback_setup["writes"]) == 3


def test_separate_gateway_and_authorized_actor(database, settings, upstream, rollback_setup, monkeypatch):
    bundle = draft(database, settings)
    approve(database, settings, bundle.fingerprint)
    for name in type(settings).model_fields:
        monkeypatch.setattr(api_settings, name, getattr(settings, name))
    monkeypatch.setattr(app.state, "sessions", database, raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    payload = {"bundle_fingerprint": bundle.fingerprint, "application": "viseca-prod",
               "actor": "manager", "request_id": "api-1"}
    assert client.post("/rollbacks", json=payload).status_code == 401
    assert client.post("/rollbacks", json=payload,
                       headers={"X-Rollback-Gateway-Secret": "gateway-only"}).status_code == 401
    response = client.post("/rollbacks", json={**payload, "actor": "outsider"},
                           headers={"X-Rollback-Gateway-Secret": "rollback-only"})
    assert response.status_code == 403
    response = client.post("/rollbacks", json=payload, headers={"X-Rollback-Gateway-Secret": "rollback-only"})
    assert response.status_code == 200 and response.json()["status"] == "draft"
    assert not rollback_setup["writes"]
