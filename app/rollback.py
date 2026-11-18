import base64
import difflib
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.agents.query_agent import _ref_valid
from app.release_bundles import (
    BundleError, _revalidate_for_approval, authorize_actor, read_bundle, validate_bundle_settings,
)
from app.storage.models import AuditEvent, RollbackPlan
from app.tools.gitlab_client import GitLabClient
from app.traceability import SHA
from config.settings import Settings

logger = logging.getLogger(__name__)


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    branch: str = Field(min_length=1, max_length=200)
    files: list[str] = Field(min_length=1, max_length=20)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    application: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class RollbackActor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=256)


class RollbackApproval(RollbackActor):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class RollbackFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    before_sha256: str
    after_sha256: str


class RollbackSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bundle_fingerprint: str
    application: str
    project: str
    project_id: int
    target_branch: str
    target_commit: str
    baseline_commit: str
    files: list[RollbackFile] = Field(min_length=1, max_length=20)


class RollbackView(BaseModel):
    id: str
    request_id: str
    fingerprint: str
    status: Literal["draft", "approved", "executing", "uncertain", "completed"]
    plan: RollbackSnapshot
    requested_by: str
    approved_by: str | None
    merge_request_iid: int | None
    created_at: datetime


class PreviewFile(BaseModel):
    path: str
    diff: str


def _target(settings: Settings, application: str) -> Target:
    try:
        raw = json.loads(settings.rollback_targets)
        if not isinstance(raw, dict) or application not in raw:
            raise ValueError("Application is not configured")
        target = Target.model_validate(raw[application])
    except (ValueError, TypeError) as exc:
        raise BundleError(503, "Rollback targets are not configured for this application") from exc
    if not _ref_valid(target.branch) or SHA.fullmatch(target.branch):
        raise BundleError(503, "Invalid rollback target branch")
    if len(set(target.files)) != len(target.files) or any(
        len(path) > 512 or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)
        or any(part in (".", "..") for part in path.split("/"))
        for path in target.files
    ):
        raise BundleError(503, "Invalid rollback file allowlist")
    return target


def validate_rollback_settings(settings: Settings) -> None:
    validate_bundle_settings(settings)
    conflicts = {
        settings.bundle_gateway_secret, settings.query_secret, settings.status_secret,
        settings.gitlab_webhook_secret, settings.argocd_webhook_secret, settings.teams_gateway_secret,
        settings.gitlab_token, settings.argocd_token, settings.harbor_password, settings.mcp_bearer_token,
        settings.llm_azure_api_key, settings.llm_self_hosted_api_key,
    }
    if not settings.rollback_enabled or not settings.rollback_gateway_secret or settings.rollback_gateway_secret in conflicts:
        raise BundleError(503, "Rollback is disabled or not fully configured")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(snapshot: RollbackSnapshot) -> str:
    return _hash(json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode())


def _head(client: GitLabClient, project_id: int, branch: str) -> str:
    result = client.branch(project_id, branch)
    commit = result.get("commit")
    if result.get("name") != branch or not isinstance(commit, dict) or not isinstance(commit.get("id"), str):
        raise BundleError(502, "GitLab returned invalid target branch evidence")
    sha = commit["id"]
    if not SHA.fullmatch(sha):
        raise BundleError(502, "GitLab returned an invalid target commit")
    return sha


def _snapshot(record: RollbackPlan, settings: Settings, factory: sessionmaker[Session]) -> RollbackSnapshot:
    snapshot = RollbackSnapshot.model_validate(record.plan)
    if _fingerprint(snapshot) != record.fingerprint:
        raise BundleError(409, "Rollback plan no longer matches its fingerprint")
    if record.approved_by and record.approved_fingerprint != record.fingerprint:
        raise BundleError(409, "Rollback approval does not match the plan")
    bundle = read_bundle(factory, settings, snapshot.bundle_fingerprint)
    if not bundle.approval:
        raise BundleError(409, "The selected bundle is not approved")
    env = next((entry for entry in bundle.manifest.environments if entry.application == snapshot.application), None)
    target = _target(settings, snapshot.application)
    if (
        env is None or env.config_project != snapshot.project or env.config_project_id != snapshot.project_id
        or env.config_revision != snapshot.baseline_commit or target.branch != snapshot.target_branch
        or any(item.path not in target.files for item in snapshot.files)
        or len({item.path for item in snapshot.files}) != len(snapshot.files)
        or not SHA.fullmatch(snapshot.target_commit)
    ):
        raise BundleError(403, "Rollback plan is no longer within the authorized scope")
    return snapshot


def _view(record: RollbackPlan, snapshot: RollbackSnapshot) -> RollbackView:
    return RollbackView(
        id=record.id, request_id=record.request_id, fingerprint=record.fingerprint, status=record.status,
        plan=snapshot, requested_by=record.requested_by, approved_by=record.approved_by,
        merge_request_iid=record.merge_request_iid, created_at=record.created_at,
    )


def _record(factory: sessionmaker[Session], settings: Settings, plan_id: str) -> RollbackView:
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise BundleError(404, "Rollback plan not found")
    with factory() as session:
        record = session.get(RollbackPlan, plan_id)
        if not record:
            raise BundleError(404, "Rollback plan not found")
        return _view(record, _snapshot(record, settings, factory))


def _files(client: GitLabClient, snapshot: RollbackSnapshot) -> list[tuple[RollbackFile, bytes, bytes]]:
    files = []
    for item in snapshot.files:
        before = client.repository_file(snapshot.project_id, item.path, snapshot.target_commit)
        after = client.repository_file(snapshot.project_id, item.path, snapshot.baseline_commit)
        if _hash(before) != item.before_sha256 or _hash(after) != item.after_sha256:
            raise BundleError(409, "Rollback file evidence changed")
        try:
            before.decode("utf-8")
            after.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BundleError(422, "Rollback supports UTF-8 text files only") from exc
        files.append((item, before, after))
    return files


def create_plan(factory: sessionmaker[Session], settings: Settings, request: RollbackRequest) -> RollbackView:
    validate_rollback_settings(settings)
    authorize_actor(settings, request.actor)
    with factory() as session:
        old = session.scalar(select(RollbackPlan).where(RollbackPlan.request_id == request.request_id))
        if old:
            snapshot = _snapshot(old, settings, factory)
            if old.requested_by != request.actor or snapshot.bundle_fingerprint != request.bundle_fingerprint \
                    or snapshot.application != request.application:
                raise BundleError(409, "Rollback request ID is already in use")
            return _view(old, snapshot)
    bundle = read_bundle(factory, settings, request.bundle_fingerprint)
    if not bundle.approval:
        raise BundleError(409, "The selected bundle is not approved")
    env = next((entry for entry in bundle.manifest.environments if entry.application == request.application), None)
    if not env:
        raise BundleError(404, "Application is not included in the bundle")
    target = _target(settings, request.application)
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        if client.project_id(env.config_project) != env.config_project_id:
            raise BundleError(409, "Configuration project identity changed")
        head = _head(client, env.config_project_id, target.branch)
        files = []
        for path in target.files:
            before = client.repository_file(env.config_project_id, path, head)
            after = client.repository_file(env.config_project_id, path, env.config_revision)
            try:
                before.decode("utf-8")
                after.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BundleError(422, "Rollback supports UTF-8 text files only") from exc
            if before != after:
                files.append(RollbackFile(path=path, before_sha256=_hash(before), after_sha256=_hash(after)))
        if not files:
            raise BundleError(409, "No allowlisted files differ from the selected bundle")
        snapshot = RollbackSnapshot(
            bundle_fingerprint=request.bundle_fingerprint, application=request.application,
            project=env.config_project, project_id=env.config_project_id, target_branch=target.branch,
            target_commit=head, baseline_commit=env.config_revision, files=files,
        )
    finally:
        client.close()
    try:
        with factory.begin() as session:
            record = RollbackPlan(id=uuid4().hex, request_id=request.request_id, fingerprint=_fingerprint(snapshot),
                                  plan=snapshot.model_dump(mode="json"), requested_by=request.actor, status="draft")
            session.add(record)
            session.add(AuditEvent(kind="rollback_proposed", reference=record.id,
                                   details={"actor": request.actor, "fingerprint": record.fingerprint}))
            session.flush()
            return _view(record, snapshot)
    except IntegrityError as exc:
        raise BundleError(409, "Rollback request conflicts with another request; retry") from exc


def read_plan(factory: sessionmaker[Session], settings: Settings, plan_id: str, actor: str) -> RollbackView:
    validate_rollback_settings(settings)
    authorize_actor(settings, actor)
    return _record(factory, settings, plan_id)


def preview_plan(factory: sessionmaker[Session], settings: Settings, plan_id: str, actor: str) -> list[PreviewFile]:
    plan = read_plan(factory, settings, plan_id, actor).plan
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        if client.project_id(plan.project) != plan.project_id:
            raise BundleError(409, "Configuration project identity changed")
        files = _files(client, plan)
    finally:
        client.close()
    result = []
    for item, before, after in files:
        lines = difflib.unified_diff(
            before.decode("utf-8").splitlines(keepends=True),
            after.decode("utf-8").splitlines(keepends=True),
            fromfile=f"target/{item.path}", tofile=f"rollback/{item.path}",
        )
        diff = "".join(line if line.endswith(("\n", "\r")) else
                       f"{line}\n\\ No newline at end of file\n" for line in lines)
        result.append(PreviewFile(path=item.path, diff=diff))
    return result


def approve_plan(factory: sessionmaker[Session], settings: Settings, plan_id: str, actor: str,
                 fingerprint: str) -> RollbackView:
    current = read_plan(factory, settings, plan_id, actor)
    if current.fingerprint != fingerprint:
        raise BundleError(409, "Approval fingerprint does not match the rollback plan")
    if current.status != "draft":
        if current.approved_by == actor and current.status in {"approved", "executing", "uncertain", "completed"}:
            return current
        raise BundleError(409, "Rollback plan has already been processed")
    with factory.begin() as session:
        result = session.execute(update(RollbackPlan).where(
            RollbackPlan.id == plan_id, RollbackPlan.status == "draft", RollbackPlan.fingerprint == fingerprint,
        ).values(status="approved", approved_by=actor, approved_fingerprint=fingerprint))
        if result.rowcount != 1:
            raise BundleError(409, "Rollback approval conflicts with another request")
        session.add(AuditEvent(kind="rollback_approved", reference=plan_id,
                               details={"actor": actor, "fingerprint": fingerprint}))
    return _record(factory, settings, plan_id)


def execute_plan(factory: sessionmaker[Session], settings: Settings, plan_id: str, actor: str) -> RollbackView:
    current = read_plan(factory, settings, plan_id, actor)
    if not settings.write_enabled:
        raise BundleError(403, "GitLab write operations are disabled")
    if current.status == "completed":
        return current
    if current.status != "approved" or not current.approved_by:
        raise BundleError(409, "Rollback requires an approved, unexecuted plan")
    authorize_actor(settings, current.approved_by)
    bundle = read_bundle(factory, settings, current.plan.bundle_fingerprint)
    _revalidate_for_approval(settings, bundle.manifest)
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token, True)
    try:
        plan = current.plan
        if client.project_id(plan.project) != plan.project_id or _head(client, plan.project_id, plan.target_branch) != plan.target_commit:
            raise BundleError(409, "Target project or branch changed; create a new rollback plan")
        files = _files(client, plan)
        with factory.begin() as session:
            claimed = session.execute(update(RollbackPlan).where(
                RollbackPlan.id == plan_id, RollbackPlan.status == "approved",
                RollbackPlan.fingerprint == current.fingerprint,
                RollbackPlan.approved_fingerprint == current.fingerprint,
            ).values(status="executing"))
            if claimed.rowcount != 1:
                raise BundleError(409, "Rollback has already been claimed")
            session.add(AuditEvent(kind="rollback_started", reference=plan_id,
                                   details={"actor": actor, "fingerprint": current.fingerprint}))
        try:
            if _head(client, plan.project_id, plan.target_branch) != plan.target_commit:
                raise BundleError(409, "Target branch changed before execution")
            branch = f"agent-rollback/{plan_id}"
            created = client.create_branch(plan.project_id, branch, plan.target_commit)
            if not isinstance(created, dict) or created.get("name") != branch \
                    or not isinstance(created.get("commit"), dict) \
                    or created["commit"].get("id") != plan.target_commit:
                raise BundleError(502, "GitLab returned invalid rollback branch evidence")
            actions = [{"action": "update", "file_path": item.path, "encoding": "base64",
                        "content": base64.b64encode(after).decode("ascii")} for item, _, after in files]
            commit = client.create_commit(plan.project_id, branch, f"Stage rollback {plan_id}", actions)
            if not isinstance(commit, dict) or not isinstance(commit.get("id"), str) \
                    or not SHA.fullmatch(commit["id"]) or commit.get("parent_ids") != [plan.target_commit]:
                raise BundleError(502, "GitLab returned invalid rollback commit evidence")
            if _head(client, plan.project_id, plan.target_branch) != plan.target_commit:
                raise BundleError(409, "Target branch changed before merge request creation")
            mr = client.create_merge_request(
                plan.project_id, branch, plan.target_branch, f"Draft: Rollback {plan.application}",
                description=f"Approved rollback plan {plan_id}\n\nPlan fingerprint: {current.fingerprint}\n"
                            f"Bundle fingerprint: {plan.bundle_fingerprint}\n"
                            f"Target commit at approval: {plan.target_commit}\n"
                            "Review the complete diff and revalidate the target branch before merging.",
            )
            if not isinstance(mr, dict) or type(mr.get("iid")) is not int or mr["iid"] <= 0 \
                    or mr.get("source_branch") != branch or mr.get("target_branch") != plan.target_branch \
                    or mr.get("state") != "opened" or not str(mr.get("title", "")).startswith("Draft:"):
                raise BundleError(502, "GitLab returned invalid merge request evidence")
        except (httpx.HTTPError, ValueError, BundleError) as exc:
            logger.warning("Rollback execution requires manual reconciliation: %s", type(exc).__name__)
            with factory.begin() as session:
                session.execute(update(RollbackPlan).where(
                    RollbackPlan.id == plan_id, RollbackPlan.status == "executing",
                ).values(status="uncertain"))
                session.add(AuditEvent(kind="rollback_uncertain", reference=plan_id,
                                       details={"actor": actor, "error_type": type(exc).__name__}))
            raise BundleError(502, "Rollback execution is uncertain; inspect GitLab before taking further action") from exc
        with factory.begin() as session:
            session.execute(update(RollbackPlan).where(
                RollbackPlan.id == plan_id, RollbackPlan.status == "executing",
            ).values(status="completed", merge_request_iid=mr["iid"]))
            session.add(AuditEvent(kind="rollback_mr_created", reference=plan_id,
                                   details={"actor": actor, "merge_request_iid": mr["iid"]}))
    finally:
        client.close()
    return _record(factory, settings, plan_id)
