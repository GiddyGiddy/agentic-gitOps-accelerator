import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typing_extensions import TypedDict

from app.llm.providers import create_provider, validate_url
from app.storage.models import TraceBuild, TraceDeployment, TraceFix
from app.tools.argocd_client import ArgoCDClient
from app.tools.gitlab_client import GitLabClient
from app.traceability import jira_references, validate_traceability_settings
from config.settings import Settings


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    application: str | None = Field(default=None, min_length=1, max_length=200)
    project: str | None = Field(default=None, min_length=1, max_length=300)
    from_ref: str | None = Field(default=None, min_length=1, max_length=200)
    to_ref: str | None = Field(default=None, min_length=1, max_length=200)
    jira_id: str | None = Field(default=None, min_length=1, max_length=64)
    chart_version: str | None = Field(default=None, min_length=1, max_length=128)


class DeployedVersionResult(TypedDict):
    application: str
    chart_version: str | None
    unavailable_reason: str | None
    sync_status: str | None
    health_status: str | None
    comparison_revision: str | None
    reconciled_at: str | None
    reconciliation_fresh: bool
    deployment_matches_sync: bool
    last_deployment_revision: str | None
    last_deployed_at: str | None


class ComparisonChange(TypedDict):
    old_path: str | None
    new_path: str
    new_file: bool
    deleted_file: bool
    renamed_file: bool
    version_lines: list[str]
    diff_incomplete: bool
    version_lines_truncated: bool


class BranchComparisonResult(TypedDict):
    project: str
    from_ref: str
    to_ref: str
    changes: list[ComparisonChange]
    incomplete: bool


class TraceCandidate(TypedDict):
    job_id: int
    chart_name: str
    chart_version: str
    digest: str
    harbor_url: str
    harbor_project: str
    harbor_repository: str
    harbor_tag: str
    build_commit: str
    ref: str
    manifest_sha256: str
    observed_at: datetime


class JiraEvidence(TypedDict):
    build: TraceCandidate
    fix_commit: str
    patch_path: str | None


class JiraResult(TypedDict):
    project: str
    jira_id: str
    candidates: list[JiraEvidence]
    truncated: bool


class OriginCandidate(TypedDict):
    build: TraceCandidate
    origin: Literal["planned_release", "emergency_hotfix", "ambiguous", "unknown"]
    matching_tags: list[str]


class DeploymentObservation(TypedDict):
    job_id: int
    repository: str
    sync_status: str | None
    health_status: str | None
    comparison_revision: str | None
    observed_at: datetime


class OriginResult(TypedDict):
    project: str
    application: str | None
    chart_version: str
    deployment_observations: list[DeploymentObservation]
    candidates: list[OriginCandidate]
    truncated: bool
    deployment_origin: Literal["unverified"]
    current_chart_observed: bool | None
    current_state_warning: str | None


class QueryResponse(BaseModel):
    intent: Literal["deployed_version", "branch_comparison", "jira_fix", "release_origin"]
    observed_at: datetime
    result: DeployedVersionResult | BranchComparisonResult | JiraResult | OriginResult
    evidence: list[str]
    limitations: list[str]


class DeployedVersionResponse(QueryResponse):
    intent: Literal["deployed_version"]
    result: DeployedVersionResult


class QueryError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


KEYWORDS = {
    "which", "what", "is", "the", "a", "chart", "version", "deployed", "deployment",
    "currently", "current", "right", "now", "in", "on", "for", "of", "show", "get",
    "compare", "comparison", "changed", "changes", "change", "difference", "differences",
    "between", "and", "from", "to", "branch", "branches",
    "jira", "ticket", "fix", "package", "contained", "contains", "contain",
    "planned", "emergency", "hotfix", "release", "origin", "was",
    "not", "no", "dont", "do", "rollback", "delete", "promote", "update", "create",
}
REF = r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}"
COMPARE_RE = re.compile(rf"\bbetween\s+({REF})\s+and\s+({REF})(?=$|[\s?!,])", re.IGNORECASE)
VERSION_LINE = re.compile(r"^[+-]\s*(?:version|appVersion|tag):\s*\S", re.IGNORECASE)
CHART_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def _allowlist(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _keywords(question: str) -> list[str]:
    without_paths = re.sub(r"\b[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+", " ", question)
    return sorted({word for word in re.findall(r"[a-z]+", without_paths.lower()) if word in KEYWORDS})


def _ref_valid(ref: str) -> bool:
    return bool(
        re.fullmatch(REF, ref) and not any(part in ref for part in ("..", "//", "@{"))
        and not ref.endswith(("/", ".", ".lock"))
        and not any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
    )


def _model_intent(settings: Settings, question: str) -> str:
    keywords = _keywords(question)
    if (
        not keywords or re.search(r"\b(?:don't|do not|never)\b", question, re.IGNORECASE)
        or set(keywords) & {"not", "no", "dont", "rollback", "delete", "promote", "update", "create"}
    ):
        raise QueryError(422, "Question is unsupported or ambiguous")
    if settings.llm_provider != "azure_openai":
        raise QueryError(503, "Query routing requires an Azure OpenAI provider")
    provider = create_provider(settings)
    try:
        return provider.classify_query(keywords).intent
    finally:
        provider.close()


def _application(request: QueryRequest, allowed: set[str]) -> str:
    if request.application:
        if request.application not in allowed:
            raise QueryError(403, "Application is not authorized")
        return request.application
    matches = [
        name for name in allowed
        if re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w./-])", request.question)
    ]
    if len(matches) != 1:
        raise QueryError(422, "Specify exactly one authorized application")
    return matches[0]


def _project(request: QueryRequest, allowed: set[str]) -> str:
    if request.project:
        if request.project not in allowed:
            raise QueryError(403, "Project is not authorized")
        return request.project
    if len(allowed) != 1:
        raise QueryError(422, "Specify exactly one authorized project")
    return next(iter(allowed))


def _refs(request: QueryRequest) -> tuple[str, str]:
    from_ref, to_ref = request.from_ref, request.to_ref
    if not from_ref and not to_ref:
        match = COMPARE_RE.search(request.question)
        if match:
            from_ref, to_ref = match.groups()
    if not from_ref or not to_ref or not _ref_valid(from_ref) or not _ref_valid(to_ref):
        raise QueryError(422, "Specify valid from_ref and to_ref, or use 'between <ref> and <ref>'")
    return from_ref, to_ref


def _project_from_repo(repo_url: str, settings: Settings, allowed: set[str]) -> str | None:
    repo = urlsplit(repo_url)
    base = urlsplit(settings.gitlab_url)
    if (
        repo.scheme != "https" or repo.username or repo.password or repo.query or repo.fragment
        or (repo.scheme, repo.netloc) != (base.scheme, base.netloc)
    ):
        return None
    base_path = base.path.rstrip("/")
    if base_path and not repo.path.startswith(base_path + "/"):
        return None
    path = repo.path[len(base_path):].strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path if path in allowed else None


def _version_from_git(source: dict, revision: str, settings: Settings, projects: set[str]) -> tuple[str | None, str | None]:
    repo_url, path = source.get("repoURL"), source.get("path")
    if not isinstance(repo_url, str) or not isinstance(path, str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
        return None, None
    project = _project_from_repo(repo_url, settings, projects)
    if not project or path.startswith("/") or (
        path != "." and any(part in ("", ".", "..") for part in path.split("/"))
    ):
        return None, None
    file_path = "Chart.yaml" if path == "." else f"{path}/Chart.yaml"
    gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        try:
            content = gitlab.repository_file(project, file_path, revision)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None, None
            raise
    finally:
        gitlab.close()
    document = yaml.load(content, Loader=yaml.BaseLoader)
    version = document.get("version") if isinstance(document, dict) else None
    if not isinstance(version, str) or not CHART_VERSION.fullmatch(version):
        return None, None
    link = f"{settings.gitlab_url.rstrip('/')}/{quote(project, safe='/')}/-/blob/{revision}/{quote(file_path, safe='/')}"
    return version, link


def _deployed_version(settings: Settings, application: str, projects: set[str],
                      *, app_data: dict | None = None) -> DeployedVersionResponse:
    app = app_data
    if app is None:
        client = ArgoCDClient(
            settings.argocd_url, settings.argocd_token, settings.argocd_ca_cert_file
        )
        try:
            app = client.application(application)
        finally:
            client.close()
    if not isinstance(app, dict):
        raise ValueError("Invalid Argo CD application")
    spec, status = app.get("spec"), app.get("status")
    if not isinstance(spec, dict) or not isinstance(status, dict):
        raise ValueError("Invalid Argo CD application")
    sync = status.get("sync") if isinstance(status.get("sync"), dict) else {}
    health = status.get("health") if isinstance(status.get("health"), dict) else {}
    history = status.get("history")
    source = spec.get("source")
    multi_source = bool(spec.get("sources"))
    observed_at = datetime.now(timezone.utc)
    reconciled_at = status.get("reconciledAt")
    fresh = False
    if isinstance(reconciled_at, str):
        try:
            reconciled_time = datetime.fromisoformat(reconciled_at.replace("Z", "+00:00"))
            if reconciled_time.utcoffset() is not None:
                age = observed_at - reconciled_time
                fresh = timedelta(0) <= age <= timedelta(minutes=5)
        except ValueError:
            pass
    version = None
    chart_link = None
    reason = "No unambiguous successful deployment history matching the current synced revision"
    history_entry = None
    if isinstance(history, list) and history:
        dated = []
        invalid_history = False
        for item in history:
            if not isinstance(item, dict) or not isinstance(item.get("deployedAt"), str):
                invalid_history = True
                continue
            try:
                timestamp = datetime.fromisoformat(item["deployedAt"].replace("Z", "+00:00"))
                if timestamp.utcoffset() is not None:
                    dated.append((timestamp, item))
                else:
                    invalid_history = True
            except ValueError:
                invalid_history = True
        dated.sort(key=lambda pair: pair[0], reverse=True)
        if not invalid_history and dated and (len(dated) == 1 or dated[0][0] != dated[1][0]):
            history_entry = dated[0][1]
    history_source = history_entry.get("source") if history_entry else None
    revision = history_entry.get("revision") if history_entry else None
    deployment_matches_sync = bool(
        not multi_source and fresh and sync.get("status") == "Synced"
        and isinstance(source, dict) and isinstance(history_source, dict)
        and isinstance(revision, str) and revision and revision == sync.get("revision")
        and isinstance(source.get("repoURL"), str) and source["repoURL"]
        and history_source.get("repoURL") == source.get("repoURL")
        and history_source.get("chart") == source.get("chart")
        and history_source.get("path") == source.get("path")
    )
    if multi_source:
        reason = "Multi-source applications are not supported for chart version resolution"
    elif not fresh:
        reason = "Argo CD reconciliation time is missing, invalid, in the future, or more than five minutes old"
    elif deployment_matches_sync and isinstance(history_source, dict) and isinstance(revision, str):
        if (
            isinstance(history_source.get("chart"), str) and history_source["chart"]
            and CHART_VERSION.fullmatch(revision)
        ):
            version = revision
        elif history_source.get("path"):
            version, chart_link = _version_from_git(history_source, revision, settings, projects)
        reason = "" if version else "Chart version could not be resolved from the authorized GitLab source"
    evidence = [f"{settings.argocd_url.rstrip('/')}/applications/{quote(application, safe='')}"]
    if chart_link:
        evidence.append(chart_link)
    return DeployedVersionResponse(
        intent="deployed_version", observed_at=observed_at,
        result={
            "application": application, "chart_version": version, "unavailable_reason": reason or None,
            "sync_status": sync.get("status"), "health_status": health.get("status"),
            "comparison_revision": sync.get("revision"),
            "reconciled_at": reconciled_at, "reconciliation_fresh": fresh,
            "deployment_matches_sync": deployment_matches_sync,
            "last_deployment_revision": history_entry.get("revision") if history_entry else None,
            "last_deployed_at": history_entry.get("deployedAt") if history_entry else None,
        },
        evidence=evidence,
        limitations=[
            "Argo CD history and sync are not independent proof of the live workload version.",
            "Desired target revisions and Git commit SHAs are not chart versions.",
        ],
    )


def _compare(settings: Settings, project: str, from_ref: str, to_ref: str) -> QueryResponse:
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        data = client.compare(project, from_ref, to_ref)
    finally:
        client.close()
    diffs = data["diffs"]
    if not all(isinstance(item, dict) and isinstance(item.get("new_path"), str) for item in diffs):
        raise ValueError("Invalid GitLab comparison diff")
    incomplete = bool(data.get("compare_timeout") or data.get("overflow") or len(diffs) > 200)
    changes = []
    for item in diffs[:200]:
        partial = bool(item.get("too_large") or item.get("collapsed"))
        diff = item.get("diff")
        if not isinstance(diff, str):
            partial = True
            diff = ""
        incomplete = incomplete or partial
        matching_lines = [line for line in diff.splitlines() if VERSION_LINE.match(line)]
        version_lines = [line[:500] for line in matching_lines[:20]]
        changes.append({
            "old_path": item.get("old_path"), "new_path": item["new_path"],
            "new_file": bool(item.get("new_file")), "deleted_file": bool(item.get("deleted_file")),
            "renamed_file": bool(item.get("renamed_file")), "version_lines": version_lines,
            "diff_incomplete": partial,
            "version_lines_truncated": len(matching_lines) > 20 or any(len(line) > 500 for line in matching_lines[:20]),
        })
    link = (
        f"{settings.gitlab_url.rstrip('/')}/{quote(project, safe='/')}/-/compare/"
        f"{quote(from_ref, safe='')}...{quote(to_ref, safe='')}"
    )
    return QueryResponse(
        intent="branch_comparison", observed_at=datetime.now(timezone.utc),
        result={"project": project, "from_ref": from_ref, "to_ref": to_ref,
                "changes": changes, "incomplete": incomplete},
        evidence=[link],
        limitations=[
            "GitLab comparison limits can omit changes even when truncation is not reported.",
            "Version lines are textual hints, not a YAML-aware semantic diff.",
        ],
    )


def _index_project(settings: Settings, request: QueryRequest, projects: set[str]) -> tuple[str, int, tuple[str, ...]]:
    try:
        _, prefixes = validate_traceability_settings(settings)
    except ValueError as exc:
        raise QueryError(503, "Traceability is disabled or not fully configured") from exc
    project = _project(request, projects)
    if project != settings.gitlab_poll_project:
        raise QueryError(422, "Project is not configured for traceability indexing")
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        project_id = client.project_id(project)
    finally:
        client.close()
    return project, project_id, prefixes


def _candidate(build: TraceBuild) -> TraceCandidate:
    observed = build.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return {
        "job_id": build.job_id, "chart_name": build.chart_name, "chart_version": build.chart_version,
        "digest": build.digest, "harbor_url": build.harbor_url, "harbor_project": build.harbor_project,
        "harbor_repository": build.harbor_repository, "harbor_tag": build.harbor_tag,
        "build_commit": build.commit_sha, "ref": build.ref,
        "manifest_sha256": build.manifest_sha256, "observed_at": observed,
    }


def _build_link(settings: Settings, project: str, job_id: int) -> str:
    return f"{settings.gitlab_url.rstrip('/')}/{quote(project, safe='/')}/-/jobs/{job_id}"


def _provenance_links(settings: Settings, project: str, build: TraceBuild) -> set[str]:
    job = _build_link(settings, project, build.job_id)
    registry = (
        f"{build.harbor_url}/api/v2.0/projects/{quote(build.harbor_project, safe='')}"
        f"/repositories/{quote(quote(build.harbor_repository, safe=''), safe='')}/artifacts/{quote(build.harbor_tag, safe='')}"
    )
    return {job, f"{job}/artifacts/file/{quote(build.manifest_path, safe='/')}", registry}


def _jira_query(settings: Settings, request: QueryRequest, projects: set[str], factory: sessionmaker[Session]) -> QueryResponse:
    project, project_id, prefixes = _index_project(settings, request, projects)
    found = jira_references(request.jira_id or request.question, prefixes)
    if len(found) != 1 or (request.jira_id and request.jira_id.upper() not in found):
        raise QueryError(422, "Specify exactly one configured Jira reference")
    jira_id = next(iter(found))
    with factory() as session:
        rows = session.execute(
            select(TraceBuild, TraceFix).join(TraceFix, TraceFix.build_id == TraceBuild.id)
            .where(TraceBuild.source == settings.gitlab_url.rstrip("/"),
                   TraceBuild.project_id == project_id, TraceFix.jira_id == jira_id)
            .order_by(TraceBuild.observed_at.desc(), TraceBuild.id.desc(), TraceFix.id)
            .limit(101)
        ).all()
    evidence: set[str] = set()
    candidates: list[JiraEvidence] = []
    root = f"{settings.gitlab_url.rstrip('/')}/{quote(project, safe='/')}"
    for build, fix in rows[:100]:
        candidates.append({"build": _candidate(build), "fix_commit": fix.commit_sha,
                           "patch_path": fix.evidence_path or None})
        evidence.update(_provenance_links(settings, project, build))
        evidence.add(f"{root}/-/commit/{fix.commit_sha}")
        if fix.evidence_path:
            evidence.add(f"{root}/-/blob/{build.commit_sha}/{quote(fix.evidence_path, safe='/')}")
    return QueryResponse(
        intent="jira_fix", observed_at=datetime.now(timezone.utc),
        result={"project": project, "jira_id": jira_id, "candidates": candidates, "truncated": len(rows) > 100},
        evidence=sorted(evidence),
        limitations=[
            "Candidates are based on CI manifest claims and Git ancestry; they do not prove the fix is packaged or effective.",
            "Only explicitly listed fix commits and configured Jira prefixes are indexed; missing results do not prove absence.",
            "Harbor tag-to-digest verification reflects the index observation time; tags may move later.",
        ],
    )


def _origin_query(settings: Settings, request: QueryRequest, applications: set[str], projects: set[str],
                  factory: sessionmaker[Session]) -> QueryResponse:
    project, project_id, _ = _index_project(settings, request, projects)
    mentioned = [
        name for name in applications
        if re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w./-])", request.question)
    ]
    if not request.application and len(mentioned) > 1:
        raise QueryError(422, "Specify exactly one authorized application")
    application = _application(request, applications) if request.application or mentioned else None
    chart_version = request.chart_version
    current_observed: bool | None = None
    warning = None
    evidence: set[str] = set()
    if application:
        validate_url(settings.argocd_url, azure=False)
        current = _deployed_version(settings, application, projects)
        current_version = current.result["chart_version"]
        evidence.update(current.evidence)
        current_observed = current_version is not None
        if current_version and chart_version and current_version != chart_version:
            raise QueryError(422, "Specified chart version conflicts with the current Argo CD observation")
        if current_version:
            chart_version = current_version
        else:
            warning = current.result["unavailable_reason"]
    if not chart_version or not CHART_VERSION.fullmatch(chart_version):
        raise QueryError(422, "Specify a valid chart_version or an application with a resolvable current version")
    with factory() as session:
        builds = session.scalars(
            select(TraceBuild).where(
                TraceBuild.source == settings.gitlab_url.rstrip("/"),
                TraceBuild.project_id == project_id, TraceBuild.chart_version == chart_version,
            ).order_by(TraceBuild.observed_at.desc(), TraceBuild.id.desc()).limit(101)
        ).all()
        deployments: list[TraceDeployment] = []
        if application:
            deployments = list(session.scalars(select(TraceDeployment).where(
                TraceDeployment.source == settings.gitlab_url.rstrip("/"),
                TraceDeployment.project_id == project_id, TraceDeployment.application == application,
                TraceDeployment.chart_version == chart_version,
            ).order_by(TraceDeployment.observed_at.desc(), TraceDeployment.id.desc()).limit(101)).all())
    tags: list[dict] = []
    if builds:
        client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        try:
            tags = client.release_tags(project) + client.hotfix_tags(project)
        finally:
            client.close()
    if any(not isinstance(tag.get("name"), str) or not isinstance(tag.get("commit"), dict)
           or not isinstance(tag["commit"].get("id"), str) for tag in tags):
        raise ValueError("Invalid GitLab tag observation")
    candidates: list[OriginCandidate] = []
    for build in builds[:100]:
        matching = sorted(tag["name"] for tag in tags if tag["commit"]["id"] == build.commit_sha
                          and tag["name"].startswith(("REL/", "HOT/")))
        signals = set()
        if build.ref.startswith(("release/", "REL/")) or any(tag.startswith("REL/") for tag in matching):
            signals.add("planned_release")
        if build.ref.startswith(("hotfix/", "HOT/")) or any(tag.startswith("HOT/") for tag in matching):
            signals.add("emergency_hotfix")
        origin: Literal["planned_release", "emergency_hotfix", "ambiguous", "unknown"]
        if len(signals) == 2:
            origin = "ambiguous"
        elif "planned_release" in signals:
            origin = "planned_release"
        elif "emergency_hotfix" in signals:
            origin = "emergency_hotfix"
        else:
            origin = "unknown"
        candidates.append({"build": _candidate(build), "origin": origin, "matching_tags": matching})
        evidence.update(_provenance_links(settings, project, build))
        for tag in matching:
            evidence.add(f"{settings.gitlab_url.rstrip('/')}/{quote(project, safe='/')}/-/tags/{quote(tag, safe='')}")
    observations: list[DeploymentObservation] = []
    for deployment in deployments[:100]:
        stamp = deployment.observed_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        observations.append({
            "job_id": deployment.job_id, "repository": deployment.repository,
            "sync_status": deployment.sync_status, "health_status": deployment.health_status,
            "comparison_revision": deployment.comparison_revision, "observed_at": stamp,
        })
        job = _build_link(settings, project, deployment.job_id)
        evidence.update({job, f"{job}/artifacts/file/{quote(deployment.artifact_path, safe='/')}"})
    return QueryResponse(
        intent="release_origin", observed_at=datetime.now(timezone.utc),
        result={"project": project, "application": application, "chart_version": chart_version,
                "deployment_observations": observations, "candidates": candidates,
                "truncated": len(builds) > 100 or len(deployments) > 100, "deployment_origin": "unverified",
                "current_chart_observed": current_observed, "current_state_warning": warning},
        evidence=sorted(evidence),
        limitations=[
            "Build origin is a candidate based on ref names and current same-commit tags; tags and refs can move.",
            "The deployment CSV has no artifact digest, so a candidate build is not proven to be the deployed package.",
            "Index and deployment history may be incomplete; a zero count does not prove absence.",
        ],
    )


def execute_query(settings: Settings, request: QueryRequest,
                  factory: sessionmaker[Session] | None = None) -> QueryResponse:
    applications = _allowlist(settings.query_applications)
    projects = _allowlist(settings.query_projects)
    other_secrets = {
        settings.status_secret, settings.gitlab_webhook_secret,
        settings.argocd_webhook_secret, settings.teams_gateway_secret,
        settings.gitlab_token, settings.argocd_token, settings.mcp_bearer_token,
        settings.harbor_password, settings.llm_azure_api_key, settings.llm_self_hosted_api_key,
    }
    if (
        not settings.query_enabled or not settings.query_secret or not applications or not projects
        or settings.query_secret in other_secrets
    ):
        raise QueryError(503, "Query service is disabled or not fully configured")
    intent = _model_intent(settings, request.question)
    if intent == "deployed_version":
        if request.project or request.from_ref or request.to_ref or request.jira_id or request.chart_version:
            raise QueryError(422, "Unexpected comparison fields for deployed-version query")
        validate_url(settings.argocd_url, azure=False)
        validate_url(settings.gitlab_url, azure=False)
        return _deployed_version(settings, _application(request, applications), projects)
    if intent == "branch_comparison":
        if request.application or request.jira_id or request.chart_version:
            raise QueryError(422, "Unexpected application field for comparison query")
        validate_url(settings.gitlab_url, azure=False)
        project = _project(request, projects)
        return _compare(settings, project, *_refs(request))
    if intent in {"jira_fix", "release_origin"}:
        if factory is None:
            raise QueryError(503, "Traceability database is unavailable")
        if request.from_ref or request.to_ref:
            raise QueryError(422, "Unexpected comparison fields for traceability query")
        if intent == "jira_fix":
            if request.application or request.chart_version:
                raise QueryError(422, "Unexpected fields for Jira query")
            return _jira_query(settings, request, projects, factory)
        if request.jira_id:
            raise QueryError(422, "Unexpected Jira field for release origin query")
        return _origin_query(settings, request, applications, projects, factory)
    raise QueryError(422, "Question is unsupported or ambiguous")
