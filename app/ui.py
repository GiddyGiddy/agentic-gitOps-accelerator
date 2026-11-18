import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import jwt
import msal
import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.agents.query_agent import QueryRequest
from app.ingestion.chatops_handler import ApprovalRequest
from app.release_bundles import BundleApprovalRequest, BundleCreateRequest
from app.rollback import RollbackActor, RollbackApproval, RollbackRequest
from app.storage.inbox import enqueue
from app.storage.models import InboxEvent, UISession
from config.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)
COOKIE = "__Host-gitops-console"
ASSETS = Path(__file__).parent / "ui_static"
settings = get_settings()


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleInput(StrictInput):
    project: str = Field(min_length=3, max_length=300)
    job_id: int = Field(gt=0)


class RollbackInput(StrictInput):
    bundle_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    application: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class WaveInput(StrictInput):
    project_id: int = Field(gt=0)
    release: str = Field(min_length=1, max_length=128)
    wave: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class RollbackApprovalInput(StrictInput):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def _uuid(value: str) -> str:
    return str(UUID(value))


def _configuration() -> tuple[str, str, str, set[str]]:
    if not settings.ui_enabled:
        raise HTTPException(404, "Console is disabled")
    try:
        tenant = _uuid(settings.ui_entra_tenant_id)
        client = _uuid(settings.ui_entra_client_id)
        allowed = {_uuid(value.strip()) for value in settings.ui_allowed_users.split(",") if value.strip()}
        origin = settings.ui_public_origin
        url = urlsplit(origin)
        if (
            url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment or origin != f"https://{url.netloc}"
            or not settings.ui_entra_client_secret or not allowed
        ):
            raise ValueError("Invalid console configuration")
        _ = url.port
    except ValueError as exc:
        raise HTTPException(503, "Console identity configuration is incomplete or invalid") from exc
    return tenant, client, origin, allowed


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _cookie(response: Response, value: str, max_age: int, *, samesite: str = "lax") -> None:
    response.set_cookie(COOKIE, value, max_age=max_age, secure=True, httponly=True, samesite=samesite, path="/")


def _msal(tenant: str, client: str) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client, authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=settings.ui_entra_client_secret,
        timeout=10,
    )


def _verified_claims(token: str, tenant: str, client: str) -> dict:
    jwks = jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", timeout=10)
    key = jwks.get_signing_key_from_jwt(token)
    return jwt.decode(
        token, key.key, algorithms=["RS256"], audience=client,
        issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
        options={"require": ["iss", "aud", "exp", "iat", "tid", "oid"]},
    )


def _consume_flow(factory: sessionmaker[Session], token: str) -> dict[str, Any] | None:
    with factory.begin() as session:
        return session.execute(
            update(UISession).where(
                UISession.id_hash == _hash(token), UISession.kind == "login",
                UISession.expires_at > datetime.now(timezone.utc),
            ).values(kind="consumed").returning(UISession.login_flow)
        ).scalar_one_or_none()


def _save_authenticated(factory: sessionmaker[Session], old_token: str, new_token: str,
                        actor: str, expiry: datetime) -> None:
    with factory.begin() as session:
        session.execute(delete(UISession).where(UISession.id_hash == _hash(old_token)))
        session.add(UISession(
            id_hash=_hash(new_token), kind="authenticated", actor=actor,
            csrf_token=secrets.token_urlsafe(32), expires_at=expiry,
        ))


def _current(request: Request, *, mutation: bool = False, approver: bool = False) -> UISession:
    _, _, origin, allowed = _configuration()
    token = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(401, "Sign in to continue")
    with request.app.state.sessions() as session:
        current = session.scalar(select(UISession).where(
            UISession.id_hash == _hash(token), UISession.kind == "authenticated",
            UISession.expires_at > datetime.now(timezone.utc),
        ))
        if current is None or current.actor not in allowed:
            raise HTTPException(401, "Session expired or access revoked")
        if mutation:
            csrf = request.headers.get("x-console-csrf", "")
            if (
                request.headers.get("origin") != origin or not current.csrf_token
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf)
                or not hmac.compare_digest(csrf, current.csrf_token)
            ):
                raise HTTPException(403, "Invalid request origin or CSRF token")
        if approver and current.actor not in settings.approvers:
            raise HTTPException(403, "This account is not an authorized approver")
        return current


@router.get("/console", include_in_schema=False)
def console() -> FileResponse:
    _configuration()
    return FileResponse(ASSETS / "index.html")


@router.get("/console/assets/{name}", include_in_schema=False)
def asset(name: str) -> FileResponse:
    _configuration()
    if name not in {"app.js", "style.css"}:
        raise HTTPException(404, "Asset not found")
    return FileResponse(ASSETS / name)


@router.get("/console/login", include_in_schema=False)
def login(request: Request) -> RedirectResponse:
    tenant, client, origin, _ = _configuration()
    try:
        flow = _msal(tenant, client).initiate_auth_code_flow(
            [], redirect_uri=f"{origin}/console/callback", response_mode="form_post",
        )
        if not isinstance(flow.get("auth_uri"), str):
            raise ValueError("Missing authorization URL")
    except (ValueError, requests.RequestException, OSError) as exc:
        logger.warning("Unable to start Entra sign-in: %s", type(exc).__name__)
        raise HTTPException(503, "Unable to start Entra sign-in") from exc
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with request.app.state.sessions.begin() as session:
        session.execute(delete(UISession).where(UISession.expires_at < now))
        session.add(UISession(id_hash=_hash(token), kind="login", login_flow=flow,
                              expires_at=now + timedelta(minutes=10)))
    response = RedirectResponse(flow["auth_uri"], status_code=302)
    _cookie(response, token, 600, samesite="none")
    return response


@router.post("/console/callback", include_in_schema=False)
async def callback(request: Request) -> RedirectResponse:
    tenant, client, _, allowed = _configuration()
    token = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(400, "Invalid or expired sign-in session")
    flow = await run_in_threadpool(_consume_flow, request.app.state.sessions, token)
    if not flow:
        raise HTTPException(400, "Invalid or expired sign-in session")
    try:
        form = await request.form()
        if any(len(form.getlist(key)) != 1 for key in form):
            raise ValueError("Duplicate authorization response field")
        result = await run_in_threadpool(
            lambda: _msal(tenant, client).acquire_token_by_auth_code_flow(flow, dict(form))
        )
        if ("error" in result or not isinstance(result.get("id_token"), str)
                or not isinstance(result.get("id_token_claims"), dict)):
            raise ValueError("Entra did not issue an ID token")
        claims = await run_in_threadpool(_verified_claims, result["id_token"], tenant, client)
        if not claims.get("nonce") or claims["nonce"] != result["id_token_claims"].get("nonce"):
            raise ValueError("Entra token nonce mismatch")
        if _uuid(claims["tid"]) != tenant:
            raise ValueError("Unexpected tenant")
        actor = _uuid(claims["oid"])
        expiry = min(datetime.now(timezone.utc) + timedelta(hours=1),
                     datetime.fromtimestamp(claims["exp"], timezone.utc))
        if expiry <= datetime.now(timezone.utc):
            raise ValueError("Entra token expired")
    except (ValueError, KeyError, TypeError, OverflowError, RuntimeError, jwt.PyJWTError, OSError,
            requests.RequestException) as exc:
        logger.warning("Entra sign-in failed: %s", type(exc).__name__)
        raise HTTPException(401, "Entra sign-in could not be verified") from exc
    if actor not in allowed:
        raise HTTPException(403, "This account is not authorized to use the console")
    new_token = secrets.token_urlsafe(32)
    await run_in_threadpool(_save_authenticated, request.app.state.sessions, token, new_token, actor, expiry)
    response = RedirectResponse("/console", status_code=303)
    _cookie(response, new_token, max(0, int((expiry - datetime.now(timezone.utc)).total_seconds())))
    return response


@router.get("/console/api/session")
def session_info(request: Request) -> dict:
    current = _current(request)
    return {"actor": current.actor, "can_approve": current.actor in settings.approvers,
            "csrf": current.csrf_token, "query_enabled": settings.query_enabled,
            "bundle_enabled": settings.bundle_enabled, "rollback_enabled": settings.rollback_enabled,
            "write_enabled": settings.write_enabled}


@router.post("/console/api/logout")
def logout(request: Request) -> Response:
    _current(request, mutation=True)
    with request.app.state.sessions.begin() as session:
        session.execute(delete(UISession).where(UISession.id_hash == _hash(request.cookies[COOKIE])))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.post("/console/api/query")
def console_query(payload: QueryRequest, request: Request):
    _current(request, mutation=True)
    from app.main import query
    return query(payload, request, settings.query_secret)


@router.post("/console/api/bundles")
def console_create_bundle(payload: BundleInput, request: Request):
    current = _current(request, mutation=True, approver=True)
    from app.main import create_release_bundle
    return create_release_bundle(BundleCreateRequest(**payload.model_dump(), actor=current.actor),
                                 request, settings.bundle_gateway_secret)


@router.get("/console/api/bundles/{fingerprint}")
def console_bundle(fingerprint: str, request: Request):
    _current(request)
    from app.main import get_release_bundle
    return get_release_bundle(fingerprint, request, settings.query_secret)


@router.get("/console/api/bundles/{fingerprint}/drift/{application}")
def console_drift(fingerprint: str, application: str, request: Request):
    _current(request)
    from app.main import release_bundle_drift
    return release_bundle_drift(fingerprint, application, request, settings.query_secret)


@router.post("/console/api/bundles/{fingerprint}/approval")
def console_approve_bundle(fingerprint: str, request: Request):
    current = _current(request, mutation=True, approver=True)
    from app.main import approve_release_bundle
    approval = BundleApprovalRequest(actor=current.actor, request_id=f"ui:{fingerprint}:{current.actor}")
    return approve_release_bundle(fingerprint, approval, request, settings.bundle_gateway_secret)


@router.post("/console/api/rollbacks")
def console_create_rollback(payload: RollbackInput, request: Request):
    current = _current(request, mutation=True, approver=True)
    from app.main import create_rollback
    return create_rollback(RollbackRequest(**payload.model_dump(), actor=current.actor),
                           request, settings.rollback_gateway_secret)


@router.get("/console/api/rollbacks/{plan_id}")
def console_rollback(plan_id: str, request: Request):
    current = _current(request, approver=True)
    from app.main import get_rollback
    return get_rollback(plan_id, current.actor, request, settings.rollback_gateway_secret)


@router.get("/console/api/rollbacks/{plan_id}/preview")
def console_preview(plan_id: str, request: Request):
    current = _current(request, approver=True)
    from app.main import preview_rollback
    return preview_rollback(plan_id, current.actor, request, settings.rollback_gateway_secret)


@router.post("/console/api/rollbacks/{plan_id}/approval")
def console_approve_rollback(plan_id: str, payload: RollbackApprovalInput, request: Request):
    current = _current(request, mutation=True, approver=True)
    from app.main import approve_rollback
    return approve_rollback(plan_id, RollbackApproval(actor=current.actor, fingerprint=payload.fingerprint),
                            request, settings.rollback_gateway_secret)


@router.post("/console/api/rollbacks/{plan_id}/execute")
def console_execute_rollback(plan_id: str, payload: StrictInput, request: Request):
    current = _current(request, mutation=True, approver=True)
    from app.main import execute_rollback
    return execute_rollback(plan_id, RollbackActor(actor=current.actor), request, settings.rollback_gateway_secret)


@router.post("/console/api/wave-approvals", status_code=202)
def console_wave(payload: WaveInput, request: Request):
    current = _current(request, mutation=True, approver=True)
    approval = ApprovalRequest(**payload.model_dump(), actor=current.actor)
    try:
        item, duplicate = enqueue(request.app.state.sessions, "console", approval.request_id,
                                  {"kind": "approval", "payload": approval.model_dump()})
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"event_id": item.id, "status": item.status, "duplicate": duplicate}


@router.get("/console/api/wave-approvals/{event_id}")
def console_wave_status(event_id: int, request: Request):
    _current(request, approver=True)
    with request.app.state.sessions() as session:
        event = session.get(InboxEvent, event_id)
        if not event or event.kind != "approval" or event.source != "console":
            raise HTTPException(404, "Console approval event not found")
        return {"event_id": event.id, "status": event.status, "result": event.result, "error": event.error}
