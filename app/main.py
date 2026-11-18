from contextlib import asynccontextmanager
import logging
from typing import Any

import httpx
import openai
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import text

from app.agents.query_agent import QueryError, QueryRequest, QueryResponse, execute_query
from app.ingestion.argocd_events import normalize_argocd_event
from app.ingestion.chatops_handler import ApprovalRequest
from app.ingestion.gitlab_webhooks import normalize_gitlab_event
from app.storage.db import create_database
from app.storage.inbox import enqueue
from app.storage.models import InboxEvent
from app.supervisor.guardrails import PolicyViolation, require_approver, verify_secret
from config.settings import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine, factory = create_database(settings.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM inbox_events LIMIT 1"))
        app.state.sessions = factory
        yield
    finally:
        engine.dispose()


app = FastAPI(title="GitOps agent control plane", lifespan=lifespan)
logger = logging.getLogger(__name__)


def authenticate(expected: str, provided: str | None) -> None:
    try:
        verify_secret(expected, provided)
    except PolicyViolation as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def accept(request: Request, source: str, delivery_id: str, event: dict[str, Any]) -> dict[str, Any]:
    try:
        item, duplicate = enqueue(request.app.state.sessions, source, delivery_id, event)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"event_id": item.id, "status": item.status, "duplicate": duplicate}


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness(request: Request) -> dict[str, str]:
    with request.app.state.sessions() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/queries", response_model=QueryResponse)
def query(payload: QueryRequest, x_query_secret: str | None = Header(default=None)) -> QueryResponse:
    if not settings.query_enabled or not settings.query_secret:
        raise HTTPException(status_code=503, detail="Query service is disabled or not fully configured")
    authenticate(settings.query_secret, x_query_secret)
    try:
        return execute_query(settings, payload)
    except QueryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except (httpx.HTTPError, openai.APIError, ValueError, yaml.YAMLError) as exc:
        logger.warning("Query failed due to upstream, configuration, or response error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Query could not be completed; check service configuration and upstream systems") from exc


@app.get("/events/{event_id}")
def event_status(
    event_id: int, request: Request, x_status_secret: str | None = Header(default=None)
) -> dict[str, Any]:
    authenticate(settings.status_secret, x_status_secret)
    with request.app.state.sessions() as session:
        event = session.get(InboxEvent, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return {"event_id": event.id, "status": event.status, "result": event.result, "error": event.error}


@app.post("/webhooks/gitlab", status_code=202)
def gitlab_event(
    payload: dict[str, Any], request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str = Header(default=""),
    x_gitlab_event_uuid: str = Header(default=""),
) -> dict[str, Any]:
    if not settings.gitlab_webhook_enabled:
        raise HTTPException(status_code=503, detail="GitLab webhook ingestion is disabled")
    authenticate(settings.gitlab_webhook_secret, x_gitlab_token)
    try:
        event = normalize_gitlab_event(x_gitlab_event, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return accept(request, "gitlab", x_gitlab_event_uuid, event)


@app.post("/webhooks/argocd", status_code=202)
def argocd_event(
    payload: dict[str, Any], request: Request,
    x_webhook_secret: str | None = Header(default=None),
    x_event_id: str = Header(default=""),
) -> dict[str, Any]:
    authenticate(settings.argocd_webhook_secret, x_webhook_secret)
    try:
        event = normalize_argocd_event(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return accept(request, "argocd", x_event_id, event)


@app.post("/chatops/approvals", status_code=202)
def approve(
    payload: ApprovalRequest, request: Request, x_gateway_secret: str | None = Header(default=None)
) -> dict[str, Any]:
    authenticate(settings.teams_gateway_secret, x_gateway_secret)
    try:
        require_approver(payload.actor, settings.approvers)
    except PolicyViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return accept(request, "teams", payload.request_id, {"kind": "approval", "payload": payload.model_dump()})
