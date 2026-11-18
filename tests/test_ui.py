from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import app.main as main
import app.ui as ui
from app.storage.db import create_database
from app.storage.migrate import migrate
from app.storage.models import InboxEvent
from config.settings import get_settings
from test_release_bundles import draft, settings, upstream

TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"
ACTOR = "33333333-3333-4333-8333-333333333333"
READER = "44444444-4444-4444-8444-444444444444"
ORIGIN = "https://console.example"


def test_client_secret_file_overrides_environment_and_rejects_bad_values(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("mounted-secret\n", encoding="utf-8")
    monkeypatch.setenv("GITOPS_UI_ENTRA_CLIENT_SECRET", "old-secret")
    monkeypatch.setenv("GITOPS_UI_ENTRA_CLIENT_SECRET_FILE", str(secret_file))
    get_settings.cache_clear()
    try:
        loaded = get_settings()
        assert loaded.ui_entra_client_secret == "mounted-secret"
        assert "mounted-secret" not in repr(loaded)
        secret_file.write_text(" bad-secret ", encoding="utf-8")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="Entra client secret file"):
            get_settings()
        secret_file.unlink()
        get_settings.cache_clear()
        with pytest.raises(FileNotFoundError):
            get_settings()
    finally:
        get_settings.cache_clear()


@pytest.fixture
def console(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'ui.db'}"
    migrate(url)
    engine, factory = create_database(url)
    monkeypatch.setattr(main.app.state, "sessions", factory, raising=False)
    for name, value in {
        "ui_enabled": True, "ui_public_origin": ORIGIN, "ui_entra_tenant_id": TENANT,
        "ui_entra_client_id": CLIENT, "ui_entra_client_secret": "server-only",
        "ui_allowed_users": f"{ACTOR},{READER}", "teams_approvers": ACTOR,
        "query_enabled": True, "query_secret": "query-server-secret",
        "bundle_enabled": True, "rollback_enabled": True, "write_enabled": True,
    }.items():
        monkeypatch.setattr(main.settings, name, value)
    actor = {"value": ACTOR}

    class FakeMSAL:
        def initiate_auth_code_flow(self, scopes, redirect_uri, response_mode):
            assert scopes == [] and redirect_uri == f"{ORIGIN}/console/callback"
            assert response_mode == "form_post"
            return {"auth_uri": "https://login.microsoftonline.com/authorize", "state": "expected"}

        def acquire_token_by_auth_code_flow(self, flow, response):
            if response.get("state") != flow["state"]:
                raise ValueError("state mismatch")
            return {"id_token": "signed-token", "id_token_claims": {"nonce": "nonce"}}

    monkeypatch.setattr(ui, "_msal", lambda *args: FakeMSAL())
    monkeypatch.setattr(ui, "_verified_claims", lambda *args: {
        "tid": TENANT, "oid": actor["value"], "nonce": "nonce",
        "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    })
    client = TestClient(main.app, base_url=ORIGIN, follow_redirects=False)
    yield client, factory, actor
    client.close()
    engine.dispose()


def sign_in(client):
    response = client.get("/console/login")
    assert response.status_code == 302
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    old_cookie = client.cookies.get(ui.COOKIE)
    response = client.post("/console/callback", data={"code": "example", "state": "expected"})
    assert response.status_code == 303
    assert old_cookie != client.cookies.get(ui.COOKIE)
    return old_cookie


def csrf(client):
    response = client.get("/console/api/session")
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-Console-CSRF": response.json()["csrf"]}


def test_login_is_one_time_and_console_is_not_public(console):
    client, _, _ = console
    assert client.get("/console/api/session").status_code == 401
    assert client.get("/console").status_code == 200
    old_cookie = sign_in(client)
    assert client.get("/console/api/session").json()["actor"] == ACTOR
    replay = client.post("/console/callback", data={"code": "example", "state": "expected"},
                         headers={"Cookie": f"{ui.COOKIE}={old_cookie}"})
    assert replay.status_code == 400
    response = client.post("/console/api/logout", headers=csrf(client))
    assert response.status_code == 204
    assert client.get("/console/api/session").status_code == 401


def test_invalid_state_or_unapproved_user_cannot_sign_in(console):
    client, _, actor = console
    client.get("/console/login")
    assert client.post("/console/callback", data={"code": "example", "state": "wrong"}).status_code == 401
    assert client.post("/console/callback", data={"code": "example", "state": "expected"}).status_code == 400
    actor["value"] = "55555555-5555-4555-8555-555555555555"
    client.get("/console/login")
    assert client.post("/console/callback", data={"code": "example", "state": "expected"}).status_code == 403


def test_console_fails_closed_when_disabled_or_token_expired(console, monkeypatch):
    client, _, _ = console
    monkeypatch.setattr(main.settings, "ui_enabled", False)
    assert client.get("/console").status_code == 404
    monkeypatch.setattr(main.settings, "ui_enabled", True)
    client.get("/console/login")
    monkeypatch.setattr(ui, "_verified_claims", lambda *args: {
        "tid": TENANT, "oid": ACTOR, "nonce": "nonce",
        "exp": (datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp(),
    })
    assert client.post("/console/callback", data={"code": "example", "state": "expected"}).status_code == 401
    assert client.get("/console/api/session").status_code == 401


def test_csrf_identity_and_wave_approval_boundaries(console):
    client, factory, _ = console
    sign_in(client)
    headers = csrf(client)
    body = {"project_id": 12, "release": "VN1", "wave": "wave1", "request_id": "approval-1"}
    assert client.post("/console/api/wave-approvals", json=body).status_code == 403
    assert client.post("/console/api/wave-approvals", json=body, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/console/api/wave-approvals", json={**body, "actor": READER}, headers=headers).status_code == 422
    result = client.post("/console/api/wave-approvals", json=body, headers=headers)
    assert result.status_code == 202 and result.json()["status"] == "pending"
    with factory() as session:
        record = session.get(InboxEvent, result.json()["event_id"])
        assert record.source == "console" and record.payload["actor"] == ACTOR
    status = client.get(f"/console/api/wave-approvals/{result.json()['event_id']}")
    assert status.json()["status"] == "pending"


def test_query_and_rollback_approval_use_server_identity(console, monkeypatch):
    client, _, _ = console
    sign_in(client)
    headers = csrf(client)
    calls = {}

    def query(payload, request, secret):
        calls["query"] = (payload.question, secret)
        return {"intent": "deployed_version", "result": {}, "evidence": [], "limitations": [], "observed_at": "now"}

    def approve(plan_id, payload, request, secret):
        calls["approval"] = (plan_id, payload.actor, payload.fingerprint, secret)
        return {"status": "approved"}

    monkeypatch.setattr(main, "query", query)
    monkeypatch.setattr(main, "approve_rollback", approve)
    monkeypatch.setattr(main.settings, "rollback_gateway_secret", "server-rollback-secret")
    response = client.post("/console/api/query", json={"question": "which chart version"}, headers=headers)
    assert response.status_code == 200
    assert calls["query"] == ("which chart version", "query-server-secret")
    fingerprint = "a" * 64
    response = client.post("/console/api/rollbacks/plan/approval", json={"fingerprint": fingerprint}, headers=headers)
    assert response.status_code == 200
    assert calls["approval"] == ("plan", ACTOR, fingerprint, "server-rollback-secret")
    assert "server-rollback-secret" not in response.text


def test_console_approves_real_bundle_with_signed_in_actor(console, settings, upstream, monkeypatch):
    client, factory, _ = console
    settings.teams_approvers = f"manager,{ACTOR}"
    for name in (
        "gitlab_url", "gitlab_token", "gitlab_poll_project", "harbor_url", "harbor_username",
        "harbor_password", "traceability_enabled", "traceability_build_jobs", "traceability_jira_prefixes",
        "traceability_harbor_project", "traceability_harbor_repository", "query_enabled", "query_secret",
        "query_applications", "query_projects", "bundle_enabled", "bundle_gateway_secret",
        "teams_approvers", "bundle_config_projects", "bundle_image_repositories",
    ):
        monkeypatch.setattr(main.settings, name, getattr(settings, name))
    bundle = draft(factory, settings)
    sign_in(client)
    response = client.post(f"/console/api/bundles/{bundle.fingerprint}/approval", json={}, headers=csrf(client))
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["approval"]["actor"] == ACTOR


def test_reader_cannot_approve_and_revoked_user_loses_access(console, monkeypatch):
    client, _, actor = console
    actor["value"] = READER
    sign_in(client)
    assert client.get("/console/api/session").json()["can_approve"] is False
    response = client.post("/console/api/rollbacks/plan/approval", json={"fingerprint": "a" * 64}, headers=csrf(client))
    assert response.status_code == 403
    monkeypatch.setattr(main.settings, "ui_allowed_users", ACTOR)
    assert client.get("/console/api/session").status_code == 401


def test_entra_signature_audience_and_issuer_validation(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(ui.jwt, "PyJWKClient", lambda *args, **kwargs: SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
    ))
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT,
        "tid": TENANT, "oid": ACTOR, "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    assert ui._verified_claims(token, TENANT, CLIENT)["oid"] == ACTOR
    with pytest.raises(jwt.InvalidAudienceError):
        ui._verified_claims(token, TENANT, ACTOR)
    with pytest.raises(jwt.InvalidIssuerError):
        ui._verified_claims(jwt.encode({**claims, "iss": "https://evil.example"}, key, algorithm="RS256"), TENANT, CLIENT)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        ui._verified_claims(jwt.encode(claims, other_key, algorithm="RS256"), TENANT, CLIENT)
