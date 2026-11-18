import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app, settings as api_settings
from app.release_bundles import (
    BundleApprovalRequest, BundleCreateRequest, BundleError, approve_bundle, compare_bundle,
    create_bundle, read_bundle,
)
from app.storage.db import create_database
from app.storage.migrate import migrate
from app.storage.models import AuditEvent, BundleApproval, ReleaseBundle
from app.traceability import index_build
from config.settings import Settings


BUILD_SHA = "a" * 40
CONFIG_SHA = "b" * 40
OTHER_SHA = "c" * 40
CHART_DIGEST = "sha256:" + "d" * 64
IMAGE_DIGEST = "sha256:" + "e" * 64
OTHER_DIGEST = "sha256:" + "f" * 64


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'bundles.db'}"
    migrate(url)
    engine, factory = create_database(url)
    yield factory
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(
        gitlab_url="https://gitlab.example", gitlab_token="gitlab-token", gitlab_poll_project="team/chart",
        harbor_url="https://harbor.example", harbor_username="user", harbor_password="password",
        traceability_enabled=True, traceability_build_jobs="publish", traceability_jira_prefixes="GVH",
        traceability_harbor_project="charts", traceability_harbor_repository="chart",
        query_enabled=True, query_secret="query-only", query_applications="viseca-prod",
        query_projects="team/chart,team/config", argocd_url="https://argo.example", argocd_token="argo-token",
        bundle_enabled=True, bundle_gateway_secret="gateway-only", teams_approvers="manager,reviewer",
        bundle_config_projects="team/config", bundle_image_repositories="team/backend",
    )


@pytest.fixture
def upstream(monkeypatch):
    data = {
        "job": {"id": 42, "status": "success", "name": "publish", "ref": "release/VN1",
                "commit": {"id": BUILD_SHA}, "pipeline": {"id": 100, "sha": BUILD_SHA}},
        "manifest": {
            "schema_version": 2, "commit_sha": BUILD_SHA, "fixes": [],
            "chart": {"name": "app", "version": "260801.5", "harbor_project": "charts",
                      "harbor_repository": "chart", "tag": "260801.5", "digest": CHART_DIGEST},
            "environments": [{
                "application": "viseca-prod", "config_project": "team/config", "config_revision": CONFIG_SHA,
                "images": [{"repository": "team/backend", "digest": IMAGE_DIGEST}],
            }],
        },
        "project_ids": {"team/chart": 12, "team/config": 13},
        "chart_digest": CHART_DIGEST, "image_digest": IMAGE_DIGEST,
        "version": "260801.5", "revision": CONFIG_SHA, "sync": "Synced",
        "reconciled": datetime.now(timezone.utc).isoformat(),
        "images": [f"harbor.example/team/backend@{IMAGE_DIGEST}"],
        "multi": False, "history_missing": False, "history_mismatch": False, "harbor_calls": [],
    }

    class GitLab:
        def __init__(self, *args):
            pass

        def project_id(self, project):
            return data["project_ids"][project]

        def job(self, project, job):
            assert (project, job) == (12, 42)
            return data["job"]

        def artifact_file(self, project, job, path, *, max_bytes):
            assert (project, job, path, max_bytes) == (12, 42, "gitops-provenance.json", 65536)
            return json.dumps(data["manifest"]).encode()

        def commit(self, project, sha):
            assert project == "team/config"
            return {"id": sha}

        def repository_file(self, project, path, ref):
            assert project == "team/config"
            assert path == "chart/Chart.yaml"
            return f"version: {data['version']}\n".encode()

        def close(self):
            pass

    class Harbor:
        def __init__(self, *args):
            pass

        def artifact(self, project, repository, reference):
            data["harbor_calls"].append((project, repository, reference))
            if (project, repository) == ("charts", "chart"):
                return {"digest": data["chart_digest"]}
            if (project, repository) == ("team", "backend"):
                return {"digest": data["image_digest"]}
            raise AssertionError("Unexpected repository")

        def close(self):
            pass

    class Argo:
        def __init__(self, *args):
            pass

        def application(self, name):
            assert name == "viseca-prod"
            source = {"repoURL": "https://gitlab.example/team/config.git", "path": "chart"}
            spec = {"source": source}
            if data["multi"]:
                spec["sources"] = [source]
            return {
                "spec": spec, "status": {
                    "reconciledAt": data["reconciled"], "health": {"status": "Healthy"},
                    "sync": {"status": data["sync"], "revision": data["revision"]},
                    "summary": {"images": data["images"]},
                    "history": [] if data["history_missing"] else [{
                        "source": source,
                        "revision": OTHER_SHA if data["history_mismatch"] else data["revision"],
                        "deployedAt": data["reconciled"],
                    }],
                },
            }

        def close(self):
            pass

    for path in ("app.traceability.GitLabClient", "app.release_bundles.GitLabClient",
                 "app.agents.query_agent.GitLabClient"):
        monkeypatch.setattr(path, GitLab)
    monkeypatch.setattr("app.traceability.HarborClient", Harbor)
    monkeypatch.setattr("app.release_bundles.HarborClient", Harbor)
    monkeypatch.setattr("app.release_bundles.ArgoCDClient", Argo)
    return data


def draft(database, settings):
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    return create_bundle(database, settings, BundleCreateRequest(project="team/chart", job_id=42, actor="manager"))


def approve(database, settings, fingerprint):
    return approve_bundle(database, settings, fingerprint,
                          BundleApprovalRequest(actor="reviewer", request_id="approval-1"))


def test_bundle_fingerprint_approval_and_idempotency(database, settings, upstream):
    bundle = draft(database, settings)
    assert bundle.status == "draft"
    assert len(bundle.fingerprint) == 64
    assert bundle.manifest.environments[0].config_project_id == 13
    assert ("charts", "chart", CHART_DIGEST) in upstream["harbor_calls"]
    assert ("team", "backend", IMAGE_DIGEST) in upstream["harbor_calls"]
    same = create_bundle(database, settings, BundleCreateRequest(project="team/chart", job_id=42, actor="manager"))
    assert same.fingerprint == bundle.fingerprint
    result = approve(database, settings, bundle.fingerprint)
    assert result.status == "approved"
    assert result.approval.actor == "reviewer"
    assert approve(database, settings, bundle.fingerprint).approval.request_id == "approval-1"
    with database() as session:
        assert len(session.scalars(select(ReleaseBundle)).all()) == 1
        assert len(session.scalars(select(BundleApproval)).all()) == 1
        assert {event.kind for event in session.scalars(select(AuditEvent)).all()} == {
            "bundle_created", "bundle_approved",
        }
    with pytest.raises(BundleError) as error:
        approve_bundle(database, settings, bundle.fingerprint,
                       BundleApprovalRequest(actor="manager", request_id="different"))
    assert error.value.status_code == 409


@pytest.mark.parametrize("failure", ["manifest_changed", "artifact_missing", "project_replaced",
                                      "job_changed", "unauthorized_actor"])
def test_approval_revalidates_evidence_and_never_approves_failed_checks(
    database, settings, upstream, failure
):
    bundle = draft(database, settings)
    if failure == "manifest_changed":
        upstream["manifest"]["chart"]["version"] = "260801.6"
    elif failure == "artifact_missing":
        upstream["image_digest"] = OTHER_DIGEST
    elif failure == "project_replaced":
        upstream["project_ids"]["team/config"] = 99
    elif failure == "job_changed":
        upstream["job"]["status"] = "failed"
    with pytest.raises(BundleError):
        approve_bundle(database, settings, bundle.fingerprint, BundleApprovalRequest(
            actor="outsider" if failure == "unauthorized_actor" else "reviewer", request_id="approval-1",
        ))
    with database() as session:
        assert session.scalars(select(BundleApproval)).all() == []


@pytest.mark.parametrize("failure", ["old_manifest", "manifest_changed", "image_mismatch",
                                      "unauthorized_project", "unauthorized_image", "duplicate_application"])
def test_invalid_candidate_does_not_create_bundle(database, settings, upstream, failure):
    if failure == "old_manifest":
        upstream["manifest"]["schema_version"] = 1
        upstream["manifest"].pop("environments")
    elif failure == "unauthorized_project":
        upstream["manifest"]["environments"][0]["config_project"] = "team/other"
    elif failure == "unauthorized_image":
        upstream["manifest"]["environments"][0]["images"][0]["repository"] = "team/other"
    elif failure == "duplicate_application":
        upstream["manifest"]["environments"].append(upstream["manifest"]["environments"][0])
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    if failure == "manifest_changed":
        upstream["manifest"]["chart"]["version"] = "260801.6"
    elif failure == "image_mismatch":
        upstream["image_digest"] = OTHER_DIGEST
    with pytest.raises(BundleError):
        create_bundle(database, settings, BundleCreateRequest(project="team/chart", job_id=42, actor="manager"))
    with database() as session:
        assert session.scalars(select(ReleaseBundle)).all() == []


def test_tampering_and_revoked_scope_fail_closed(database, settings, upstream):
    bundle = draft(database, settings)
    approve(database, settings, bundle.fingerprint)
    settings.query_applications = "another-app"
    with pytest.raises(BundleError) as error:
        read_bundle(database, settings, bundle.fingerprint)
    assert error.value.status_code == 403
    settings.query_applications = "viseca-prod"
    with database.begin() as session:
        record = session.get(ReleaseBundle, bundle.fingerprint)
        document = dict(record.manifest)
        document["build_commit"] = OTHER_SHA
        record.manifest = document
    with pytest.raises(BundleError) as error:
        read_bundle(database, settings, bundle.fingerprint)
    assert error.value.status_code == 409


def test_drift_reports_known_mismatches_and_unknown_digest(database, settings, upstream):
    bundle = draft(database, settings)
    with pytest.raises(BundleError) as error:
        compare_bundle(database, settings, bundle.fingerprint, "viseca-prod")
    assert error.value.status_code == 409
    approve(database, settings, bundle.fingerprint)
    result = compare_bundle(database, settings, bundle.fingerprint, "viseca-prod")
    assert result.status == "unknown"
    assert result.chart_version.status == "match"
    assert result.config_revision.status == "match"
    assert result.images[0].status == "match"
    assert result.chart_digest.status == "unknown"
    upstream["revision"] = OTHER_SHA
    upstream["version"] = "260801.6"
    upstream["images"] = [f"harbor.example/team/backend@{OTHER_DIGEST}"]
    changed = compare_bundle(database, settings, bundle.fingerprint, "viseca-prod")
    assert changed.status == "drift"
    assert changed.chart_version.status == "drift"
    assert changed.config_revision.status == "drift"
    assert changed.images[0].status == "drift"


@pytest.mark.parametrize("problem", ["stale", "unsynced", "multi_source", "tag_only", "project_replaced",
                                      "history_missing", "history_mismatch"])
def test_incomplete_observations_do_not_claim_compliance(database, settings, upstream, problem):
    bundle = draft(database, settings)
    approve(database, settings, bundle.fingerprint)
    if problem == "stale":
        upstream["reconciled"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    elif problem == "unsynced":
        upstream["sync"] = "OutOfSync"
    elif problem == "multi_source":
        upstream["multi"] = True
    elif problem == "tag_only":
        upstream["images"] = ["harbor.example/team/backend:latest"]
    elif problem in {"history_missing", "history_mismatch"}:
        upstream[problem] = True
    else:
        upstream["project_ids"]["team/config"] = 99
    result = compare_bundle(database, settings, bundle.fingerprint, "viseca-prod")
    assert result.status == "unknown"
    if problem in {"stale", "unsynced", "multi_source", "history_missing", "history_mismatch"}:
        assert result.config_revision.status == "unknown"
        assert result.images[0].status == "unknown"
    elif problem == "tag_only":
        assert result.images[0].status == "unknown"
    else:
        assert result.config_revision.status == "unknown"


def test_drift_detects_digest_mismatch_even_with_a_tag_only_entry(database, settings, upstream):
    bundle = draft(database, settings)
    approve(database, settings, bundle.fingerprint)
    upstream["images"] = ["harbor.example/team/backend:latest", f"harbor.example/team/backend@{OTHER_DIGEST}"]
    assert compare_bundle(database, settings, bundle.fingerprint, "viseca-prod").images[0].status == "drift"


def test_gateway_auth_is_separate_from_query_auth(database, settings, upstream, monkeypatch):
    with database.begin() as session:
        index_build(session, settings, 12, 42)
    for name in Settings.model_fields:
        monkeypatch.setattr(api_settings, name, getattr(settings, name))
    monkeypatch.setattr(app.state, "sessions", database, raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    payload = {"project": "team/chart", "job_id": 42, "actor": "manager"}
    assert client.post("/release-bundles", json=payload).status_code == 401
    assert client.post("/release-bundles", json=payload, headers={"X-Bundle-Gateway-Secret": "query-only"}).status_code == 401
    response = client.post("/release-bundles", json=payload, headers={"X-Bundle-Gateway-Secret": "gateway-only"})
    assert response.status_code == 200
    fingerprint = response.json()["fingerprint"]
    assert client.get(f"/release-bundles/{fingerprint}").status_code == 401
    assert client.get(f"/release-bundles/{fingerprint}", headers={"X-Query-Secret": "query-only"}).status_code == 200
    approve_payload = {"actor": "reviewer", "request_id": "approval-1"}
    assert client.post(f"/release-bundles/{fingerprint}/approval", json=approve_payload,
                       headers={"X-Bundle-Gateway-Secret": "query-only"}).status_code == 401
    response = client.post(f"/release-bundles/{fingerprint}/approval", json=approve_payload,
                           headers={"X-Bundle-Gateway-Secret": "gateway-only"})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    drift = client.get(f"/release-bundles/{fingerprint}/drift/viseca-prod",
                       headers={"X-Query-Secret": "query-only"})
    assert drift.status_code == 200
    assert drift.json()["status"] == "unknown"


def test_postgres_concurrent_approval_is_idempotent(settings, upstream, monkeypatch):
    url = os.environ.get("GITOPS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set GITOPS_TEST_DATABASE_URL to an isolated empty PostgreSQL database")
    migrate(url)
    engine, factory = create_database(url)
    try:
        bundle = draft(factory, settings)
        from app.release_bundles import _revalidate_for_approval

        barrier = Barrier(2)

        def revalidate(*args):
            _revalidate_for_approval(*args)
            barrier.wait(timeout=10)

        monkeypatch.setattr("app.release_bundles._revalidate_for_approval", revalidate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: approve(factory, settings, bundle.fingerprint), range(2)))
        assert all(result.status == "approved" for result in results)
        with factory() as session:
            assert len(session.scalars(select(BundleApproval).where(
                BundleApproval.bundle_fingerprint == bundle.fingerprint
            )).all()) == 1
            assert len(session.scalars(select(AuditEvent).where(
                AuditEvent.kind == "bundle_approved", AuditEvent.reference == bundle.fingerprint
            )).all()) == 1
    finally:
        engine.dispose()
