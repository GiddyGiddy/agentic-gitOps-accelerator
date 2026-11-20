import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.agents.query_agent import _deployed_version, _project_from_repo
from app.llm.providers import validate_url
from app.storage.models import AuditEvent, BundleApproval, ReleaseBundle, TraceBuild
from app.supervisor.guardrails import PolicyViolation, require_approver
from app.tools.argocd_client import ArgoCDClient
from app.tools.gitlab_client import GitLabClient
from app.tools.harbor_client import HarborClient
from app.traceability import (
    DIGEST, SHA, BuildProvenance, ChartProvenance, EnvironmentProvenance, build_job_names,
    validate_traceability_settings,
)
from config.settings import Settings


class BundleError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class BundleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str = Field(min_length=3, max_length=300)
    job_id: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=256)


class BundleApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class BundleEnvironment(EnvironmentProvenance):
    config_project_id: int = Field(gt=0)


class BundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    source: str
    project: str
    project_id: int
    job_id: int
    job_name: str
    pipeline_id: int
    build_commit: str
    provenance_sha256: str
    provenance_path: str
    harbor_url: str
    chart: ChartProvenance
    environments: list[BundleEnvironment] = Field(min_length=1, max_length=20)


class ApprovalView(BaseModel):
    request_id: str
    actor: str
    approved_at: datetime


class BundleView(BaseModel):
    fingerprint: str
    status: Literal["draft", "approved"]
    manifest: BundleManifest
    created_by: str
    created_at: datetime
    approval: ApprovalView | None


class Check(BaseModel):
    status: Literal["match", "drift", "unknown"]
    expected: str
    observed: str | None
    reason: str


class DriftView(BaseModel):
    fingerprint: str
    application: str
    observed_at: datetime
    status: Literal["match", "drift", "unknown"]
    sync_status: str | None
    health_status: str | None
    reconciled_at: str | None
    chart_version: Check
    chart_digest: Check
    config_revision: Check
    images: list[Check]
    extra_images: list[str]
    evidence: list[str]
    limitations: list[str]


def _set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def validate_bundle_settings(settings: Settings) -> None:
    other_secrets = {
        settings.query_secret, settings.status_secret, settings.gitlab_webhook_secret,
        settings.argocd_webhook_secret, settings.teams_gateway_secret, settings.gitlab_token,
        settings.argocd_token, settings.harbor_password, settings.mcp_bearer_token,
        settings.llm_azure_api_key, settings.llm_self_hosted_api_key, settings.rollback_gateway_secret,
    }
    query_conflicts = {
        settings.status_secret, settings.gitlab_webhook_secret, settings.argocd_webhook_secret,
        settings.teams_gateway_secret, settings.bundle_gateway_secret, settings.gitlab_token,
        settings.argocd_token, settings.harbor_password, settings.mcp_bearer_token,
        settings.llm_azure_api_key, settings.llm_self_hosted_api_key, settings.rollback_gateway_secret,
    }
    if (
        not settings.bundle_enabled or not settings.bundle_gateway_secret
        or settings.bundle_gateway_secret in other_secrets
        or not settings.query_enabled or not settings.query_secret or settings.query_secret in query_conflicts
        or not settings.approvers or not _set(settings.query_projects) or not _set(settings.query_applications)
        or not _set(settings.bundle_config_projects) or not _set(settings.bundle_image_repositories)
    ):
        raise BundleError(503, "Release bundles are disabled or not fully configured")
    try:
        validate_traceability_settings(settings)
    except ValueError as exc:
        raise BundleError(503, "Release bundles require configured traceability") from exc


def authorize_actor(settings: Settings, actor: str) -> None:
    try:
        require_approver(actor, settings.approvers)
    except PolicyViolation as exc:
        raise BundleError(403, "Actor is not authorized") from exc


def _safe_path(value: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", value)
        and all(part not in (".", "..") for part in value.split("/"))
    )


def _scope(settings: Settings, manifest: BundleManifest) -> None:
    projects, applications = _set(settings.query_projects), _set(settings.query_applications)
    configs, images = _set(settings.bundle_config_projects), _set(settings.bundle_image_repositories)
    if manifest.source != settings.gitlab_url.rstrip("/") or manifest.harbor_url != settings.harbor_url.rstrip("/"):
        raise BundleError(403, "Bundle source is no longer authorized")
    if (
        manifest.project != settings.gitlab_poll_project or manifest.project not in projects
        or manifest.chart.harbor_project != settings.traceability_harbor_project
        or manifest.chart.harbor_repository != settings.traceability_harbor_repository
    ):
        raise BundleError(403, "Bundle project or chart is not authorized")
    seen_apps: set[str] = set()
    total_images = 0
    for env in manifest.environments:
        if (
            env.application not in applications or env.application in seen_apps
            or env.config_project not in configs or env.config_project not in projects
            or not _safe_path(env.config_project) or not SHA.fullmatch(env.config_revision)
        ):
            raise BundleError(403, "Bundle contains an unauthorized or invalid environment")
        seen_apps.add(env.application)
        seen_images: set[str] = set()
        for image in env.images:
            if (
                image.repository not in images or not _safe_path(image.repository)
                or not DIGEST.fullmatch(image.digest) or image.repository in seen_images
            ):
                raise BundleError(403, "Bundle contains an unauthorized or invalid image")
            seen_images.add(image.repository)
            total_images += 1
    if total_images > 100:
        raise BundleError(422, "Bundle exceeds the image limit")


def _canonical(manifest: BundleManifest) -> bytes:
    return json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fingerprint(manifest: BundleManifest) -> str:
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def _validated(record: ReleaseBundle, settings: Settings) -> BundleManifest:
    manifest = BundleManifest.model_validate(record.manifest)
    if record.fingerprint != _fingerprint(manifest):
        raise BundleError(409, "Stored bundle no longer matches its approval fingerprint")
    _scope(settings, manifest)
    return manifest


def _view(record: ReleaseBundle, manifest: BundleManifest, approval: BundleApproval | None) -> BundleView:
    return BundleView(
        fingerprint=record.fingerprint, status="approved" if approval else "draft",
        manifest=manifest, created_by=record.created_by, created_at=record.created_at,
        approval=ApprovalView(request_id=approval.request_id, actor=approval.actor,
                              approved_at=approval.approved_at) if approval else None,
    )


def create_bundle(factory: sessionmaker[Session], settings: Settings, request: BundleCreateRequest) -> BundleView:
    validate_bundle_settings(settings)
    authorize_actor(settings, request.actor)
    if request.project not in _set(settings.query_projects) or request.project != settings.gitlab_poll_project:
        raise BundleError(403, "Build project is not authorized")
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        project_id = client.project_id(request.project)
        with factory() as session:
            build = session.scalar(select(TraceBuild).where(
                TraceBuild.source == settings.gitlab_url.rstrip("/"),
                TraceBuild.project_id == project_id, TraceBuild.job_id == request.job_id,
            ))
        if build is None:
            raise BundleError(404, "Verified build is not indexed")
        raw = client.artifact_file(project_id, request.job_id, build.manifest_path, max_bytes=65536)
        if hashlib.sha256(raw).hexdigest() != build.manifest_sha256:
            raise BundleError(409, "Build manifest changed since it was indexed")
        provenance = BuildProvenance.model_validate_json(raw)
        chart = provenance.chart
        if (
            provenance.schema_version != 2 or not provenance.environments
            or provenance.commit_sha != build.commit_sha
            or chart.name != build.chart_name or chart.version != build.chart_version
            or chart.digest != build.digest or chart.harbor_project != build.harbor_project
            or chart.harbor_repository != build.harbor_repository or chart.tag != build.harbor_tag
        ):
            raise BundleError(409, "Indexed build does not provide matching version 2 provenance")
        manifest = BundleManifest(
            schema_version=1, source=build.source, project=build.project_path, project_id=build.project_id,
            job_id=build.job_id, job_name=build.job_name, pipeline_id=build.pipeline_id,
            build_commit=build.commit_sha, provenance_sha256=build.manifest_sha256,
            provenance_path=build.manifest_path, harbor_url=build.harbor_url,
            chart=chart, environments=[
                BundleEnvironment(**env.model_dump(), config_project_id=1) for env in provenance.environments
            ],
        )
        _scope(settings, manifest)
        checked_commits: set[tuple[str, str]] = set()
        project_ids: dict[str, int] = {}
        for env in manifest.environments:
            if env.config_project not in project_ids:
                project_ids[env.config_project] = client.project_id(env.config_project)
            env.config_project_id = project_ids[env.config_project]
            key = (env.config_project, env.config_revision)
            if key not in checked_commits:
                commit = client.commit(env.config_project, env.config_revision)
                if commit.get("id") != env.config_revision:
                    raise BundleError(409, "Configuration commit could not be verified")
                checked_commits.add(key)
    finally:
        client.close()

    harbor = HarborClient(settings.harbor_url, settings.harbor_username, settings.harbor_password)
    try:
        chart_artifact = harbor.artifact(chart.harbor_project, chart.harbor_repository, chart.digest)
        if not isinstance(chart_artifact, dict) or chart_artifact.get("digest") != chart.digest:
            raise BundleError(409, "Chart digest could not be verified in Harbor")
        checked_images: set[tuple[str, str]] = set()
        for env in manifest.environments:
            for image in env.images:
                key = (image.repository, image.digest)
                if key not in checked_images:
                    project, repository = image.repository.split("/", 1)
                    artifact = harbor.artifact(project, repository, image.digest)
                    if not isinstance(artifact, dict) or artifact.get("digest") != image.digest:
                        raise BundleError(409, "Image digest could not be verified in Harbor")
                    checked_images.add(key)
    finally:
        harbor.close()

    fingerprint = _fingerprint(manifest)
    try:
        with factory.begin() as session:
            record = session.get(ReleaseBundle, fingerprint)
            if record:
                return _view(record, _validated(record, settings), session.get(BundleApproval, fingerprint))
            record = ReleaseBundle(fingerprint=fingerprint, manifest=manifest.model_dump(mode="json"),
                                   created_by=request.actor)
            session.add(record)
            session.add(AuditEvent(kind="bundle_created", reference=fingerprint,
                                   details={"actor": request.actor, "project_id": project_id, "job_id": request.job_id}))
            session.flush()
            return _view(record, manifest, None)
    except IntegrityError:
        with factory() as session:
            record = session.get(ReleaseBundle, fingerprint)
            if record is None:
                raise
            return _view(record, _validated(record, settings), session.get(BundleApproval, fingerprint))


def read_bundle(factory: sessionmaker[Session], settings: Settings, fingerprint: str) -> BundleView:
    validate_bundle_settings(settings)
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise BundleError(404, "Bundle not found")
    with factory() as session:
        record = session.get(ReleaseBundle, fingerprint)
        if record is None:
            raise BundleError(404, "Bundle not found")
        return _view(record, _validated(record, settings), session.get(BundleApproval, fingerprint))


def _revalidate_for_approval(settings: Settings, manifest: BundleManifest) -> None:
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        if client.project_id(manifest.project) != manifest.project_id:
            raise BundleError(409, "Build project identity changed")
        job = client.job(manifest.project_id, manifest.job_id)
        commit, pipeline = job.get("commit"), job.get("pipeline")
        if (
            job.get("id") != manifest.job_id or job.get("status") != "success"
            or job.get("name") != manifest.job_name or job.get("name") not in build_job_names(settings)
            or not isinstance(commit, dict) or commit.get("id") != manifest.build_commit
            or not isinstance(pipeline, dict) or pipeline.get("id") != manifest.pipeline_id
            or pipeline.get("sha", manifest.build_commit) != manifest.build_commit
        ):
            raise BundleError(409, "Build job no longer matches the bundle")
        raw = client.artifact_file(manifest.project_id, manifest.job_id, manifest.provenance_path,
                                   max_bytes=65536)
        if hashlib.sha256(raw).hexdigest() != manifest.provenance_sha256:
            raise BundleError(409, "Build manifest changed since the bundle was created")
        checked: set[tuple[str, str]] = set()
        for env in manifest.environments:
            if client.project_id(env.config_project) != env.config_project_id:
                raise BundleError(409, "Configuration project identity changed")
            key = (env.config_project, env.config_revision)
            if key not in checked:
                if client.commit(*key).get("id") != env.config_revision:
                    raise BundleError(409, "Configuration commit could not be verified")
                checked.add(key)
    finally:
        client.close()
    harbor = HarborClient(settings.harbor_url, settings.harbor_username, settings.harbor_password)
    try:
        chart = manifest.chart
        artifact = harbor.artifact(chart.harbor_project, chart.harbor_repository, chart.digest)
        if not isinstance(artifact, dict) or artifact.get("digest") != chart.digest:
            raise BundleError(409, "Chart digest could not be verified in Harbor")
        checked_images: set[tuple[str, str]] = set()
        for env in manifest.environments:
            for image in env.images:
                key = (image.repository, image.digest)
                if key not in checked_images:
                    project, repository = image.repository.split("/", 1)
                    artifact = harbor.artifact(project, repository, image.digest)
                    if not isinstance(artifact, dict) or artifact.get("digest") != image.digest:
                        raise BundleError(409, "Image digest could not be verified in Harbor")
                    checked_images.add(key)
    finally:
        harbor.close()


def approve_bundle(factory: sessionmaker[Session], settings: Settings, fingerprint: str,
                   request: BundleApprovalRequest) -> BundleView:
    validate_bundle_settings(settings)
    authorize_actor(settings, request.actor)
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise BundleError(404, "Bundle not found")
    existing = read_bundle(factory, settings, fingerprint)
    if existing.approval:
        if existing.approval.request_id != request.request_id or existing.approval.actor != request.actor:
            raise BundleError(409, "Bundle already has a different approval")
        return existing
    _revalidate_for_approval(settings, existing.manifest)
    try:
        with factory.begin() as session:
            record = session.scalar(
                select(ReleaseBundle).where(ReleaseBundle.fingerprint == fingerprint).with_for_update()
            )
            if record is None:
                raise BundleError(404, "Bundle not found")
            manifest = _validated(record, settings)
            approval = session.get(BundleApproval, fingerprint)
            if approval:
                if approval.request_id != request.request_id or approval.actor != request.actor:
                    raise BundleError(409, "Bundle already has a different approval")
                return _view(record, manifest, approval)
            if session.scalar(select(BundleApproval).where(BundleApproval.request_id == request.request_id)):
                raise BundleError(409, "Approval request ID is already in use")
            approval = BundleApproval(bundle_fingerprint=fingerprint, request_id=request.request_id,
                                      actor=request.actor)
            session.add(approval)
            session.add(AuditEvent(kind="bundle_approved", reference=fingerprint,
                                   details={"actor": request.actor, "request_id": request.request_id}))
            session.flush()
            return _view(record, manifest, approval)
    except IntegrityError as exc:
        raise BundleError(409, "Approval conflicts with another request; retry or inspect the bundle") from exc


def _image_repo(reference: str) -> str:
    tail = reference.rsplit("/", 1)[-1]
    return reference.rsplit(":", 1)[0] if ":" in tail else reference


def compare_bundle(factory: sessionmaker[Session], settings: Settings, fingerprint: str,
                   application: str) -> DriftView:
    bundle = read_bundle(factory, settings, fingerprint)
    if application not in _set(settings.query_applications):
        raise BundleError(403, "Application is not authorized")
    if bundle.approval is None:
        raise BundleError(409, "Only explicitly approved bundles can be compared")
    env = next((entry for entry in bundle.manifest.environments if entry.application == application), None)
    if env is None:
        raise BundleError(404, "Application is not included in the bundle")
    validate_url(settings.argocd_url, azure=False)
    client = ArgoCDClient(
        settings.argocd_url, settings.argocd_token, settings.argocd_ca_cert_file
    )
    try:
        app = client.application(application)
    finally:
        client.close()
    current = _deployed_version(settings, application, _set(settings.query_projects), app_data=app)
    data = current.result
    sync, health, reconciled = data["sync_status"], data["health_status"], data["reconciled_at"]
    fresh = data["reconciliation_fresh"] and data["deployment_matches_sync"]
    observed_version = data["chart_version"]
    chart_version = Check(
        status="unknown" if not observed_version else ("match" if observed_version == bundle.manifest.chart.version else "drift"),
        expected=bundle.manifest.chart.version, observed=observed_version,
        reason="Argo CD chart version is unavailable" if not observed_version else "Argo CD history and sync comparison",
    )
    chart_digest = Check(status="unknown", expected=bundle.manifest.chart.digest, observed=None,
                         reason="Argo CD does not expose a verified deployed chart digest here")
    spec = app.get("spec", {})
    source = spec.get("source") if isinstance(spec, dict) and not spec.get("sources") else None
    repository = _project_from_repo(source.get("repoURL", ""), settings, _set(settings.bundle_config_projects)) \
        if isinstance(source, dict) and isinstance(source.get("repoURL"), str) else None
    revision = data["comparison_revision"]
    if not fresh or not repository or not isinstance(revision, str) or not SHA.fullmatch(revision):
        config_revision = Check(status="unknown", expected=env.config_revision, observed=None,
                                reason="A fresh synced Git source and matching deployment history are required")
    else:
        gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        try:
            project_id = gitlab.project_id(repository)
        finally:
            gitlab.close()
        if repository == env.config_project and project_id != env.config_project_id:
            config_revision = Check(status="unknown", expected=env.config_revision, observed=revision,
                                    reason="Configuration project identity changed")
        else:
            match = repository == env.config_project and revision == env.config_revision
            config_revision = Check(status="match" if match else "drift", expected=env.config_revision,
                                    observed=revision, reason=f"Argo CD comparison revision in {repository}")

    status = app.get("status", {})
    summary = status.get("summary", {}) if isinstance(status, dict) else {}
    raw_images = summary.get("images") if isinstance(summary, dict) else None
    image_refs: dict[str, set[str | None]] = {}
    available = False
    if fresh and isinstance(source, dict) and isinstance(raw_images, list) and all(
        isinstance(item, str) and item for item in raw_images
    ):
        available = True
        for ref in raw_images:
            repository_ref, separator, digest = ref.partition("@")
            image_refs.setdefault(_image_repo(repository_ref), set()).add(
                digest if separator and DIGEST.fullmatch(digest) else None
            )
    registry = urlsplit(bundle.manifest.harbor_url).netloc
    image_checks: list[Check] = []
    expected_repos: set[str] = set()
    for image in env.images:
        name = f"{registry}/{image.repository}"
        expected_repos.add(name)
        seen = image_refs.get(name, set())
        state: Literal["match", "drift", "unknown"]
        if not available or not seen:
            state = "unknown"
        elif any(item is not None and item != image.digest for item in seen):
            state = "drift"
        elif None in seen:
            state = "unknown"
        elif seen == {image.digest}:
            state = "match"
        else:
            state = "drift"
        observed = ", ".join(sorted(item for item in seen if item)) or None
        image_checks.append(Check(status=state, expected=f"{name}@{image.digest}", observed=observed,
                                  reason="Argo CD image summary; not independent runtime image IDs"))
    extra_images = sorted(name for name, values in image_refs.items() if name not in expected_repos and None not in values)
    unknown_extra = any(name not in expected_repos and None in values for name, values in image_refs.items())
    checks = [chart_version, chart_digest, config_revision, *image_checks]
    overall: Literal["match", "drift", "unknown"] = "drift" if extra_images or any(item.status == "drift" for item in checks) else (
        "unknown" if unknown_extra or any(item.status == "unknown" for item in checks) else "match"
    )
    return DriftView(
        fingerprint=fingerprint, application=application, observed_at=datetime.now(timezone.utc), status=overall,
        sync_status=sync, health_status=health, reconciled_at=reconciled, chart_version=chart_version,
        chart_digest=chart_digest, config_revision=config_revision, images=image_checks,
        extra_images=extra_images, evidence=current.evidence,
        limitations=[
            "Argo CD status and image summaries are not independent proof of live pod image IDs.",
            "A deployed chart digest is not exposed by this comparison; matching versions do not prove matching artifacts.",
            "Stale, unsynced, multi-source, tag-only, or missing observations are unknown, not compliant.",
        ],
    )
