import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.llm.providers import create_provider
from app.tools.argocd_client import ArgoCDClient
from app.tools.gitlab_client import GitLabClient
from config.settings import Settings


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    application: str | None = Field(default=None, min_length=1, max_length=200)
    project: str | None = Field(default=None, min_length=1, max_length=300)
    from_ref: str | None = Field(default=None, min_length=1, max_length=200)
    to_ref: str | None = Field(default=None, min_length=1, max_length=200)


class QueryResponse(BaseModel):
    intent: Literal["deployed_version", "branch_comparison"]
    observed_at: datetime
    result: dict
    evidence: list[str]
    limitations: list[str]


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
    "not", "no", "dont", "do", "rollback", "delete", "promote", "update", "create",
}
REF = r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}"
COMPARE_RE = re.compile(rf"\bbetween\s+({REF})\s+and\s+({REF})(?=$|[\s?!,])", re.IGNORECASE)
VERSION_LINE = re.compile(r"^[+-]\s*(?:version|appVersion|tag):\s*\S", re.IGNORECASE)


def _allowlist(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _keywords(question: str) -> list[str]:
    return sorted({word for word in re.findall(r"[a-z]+", question.lower()) if word in KEYWORDS})


def _ref_valid(ref: str) -> bool:
    return bool(
        re.fullmatch(REF, ref) and not any(part in ref for part in ("..", "//", "@{"))
        and not ref.endswith(("/", ".", ".lock"))
        and not any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
    )


def _model_intent(settings: Settings, question: str) -> str:
    keywords = _keywords(question)
    if not keywords or set(keywords) & {"not", "no", "dont", "rollback", "delete", "promote", "update", "create"}:
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
    if not project or path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
        return None, None
    file_path = f"{path}/Chart.yaml"
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
    document = yaml.safe_load(content)
    version = document.get("version") if isinstance(document, dict) else None
    if not isinstance(version, (str, int, float)) or isinstance(version, bool):
        return None, None
    link = f"{settings.gitlab_url.rstrip('/')}/{quote(project, safe='/')}/-/blob/{revision}/{quote(file_path, safe='/')}"
    return str(version), link


def _deployed_version(settings: Settings, application: str, projects: set[str]) -> QueryResponse:
    client = ArgoCDClient(settings.argocd_url, settings.argocd_token)
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
        for item in history:
            if not isinstance(item, dict) or not isinstance(item.get("deployedAt"), str):
                continue
            try:
                timestamp = datetime.fromisoformat(item["deployedAt"].replace("Z", "+00:00"))
                if timestamp.utcoffset() is not None:
                    dated.append((timestamp, item))
            except ValueError:
                continue
        dated.sort(key=lambda pair: pair[0], reverse=True)
        if dated and (len(dated) == 1 or dated[0][0] != dated[1][0]):
            history_entry = dated[0][1]
    if multi_source:
        reason = "Multi-source applications are not supported for chart version resolution"
    elif not fresh:
        reason = "Argo CD reconciliation time is missing, invalid, in the future, or more than five minutes old"
    elif isinstance(source, dict) and history_entry:
        history_source = history_entry.get("source")
        revision = history_entry.get("revision")
        if (
            sync.get("status") == "Synced" and isinstance(revision, str) and revision
            and revision == sync.get("revision") and isinstance(history_source, dict)
            and isinstance(source.get("repoURL"), str) and source["repoURL"]
            and history_source.get("repoURL") == source.get("repoURL")
            and history_source.get("chart") == source.get("chart")
            and history_source.get("path") == source.get("path")
        ):
            if (
                isinstance(history_source.get("chart"), str) and history_source["chart"]
                and not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision)
            ):
                version = revision
            elif history_source.get("path"):
                version, chart_link = _version_from_git(history_source, revision, settings, projects)
            reason = "" if version else "Chart version could not be resolved from the authorized GitLab source"
    evidence = [f"{settings.argocd_url.rstrip('/')}/applications/{quote(application, safe='')}"]
    if chart_link:
        evidence.append(chart_link)
    return QueryResponse(
        intent="deployed_version", observed_at=observed_at,
        result={
            "application": application, "chart_version": version, "unavailable_reason": reason or None,
            "sync_status": sync.get("status"), "health_status": health.get("status"),
            "comparison_revision": sync.get("revision"),
            "reconciled_at": reconciled_at, "reconciliation_fresh": fresh,
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
        incomplete = incomplete or partial
        diff = item.get("diff")
        if not isinstance(diff, str):
            incomplete = True
            diff = ""
        version_lines = [line[:500] for line in diff.splitlines() if VERSION_LINE.match(line)][:20]
        changes.append({
            "old_path": item.get("old_path"), "new_path": item["new_path"],
            "new_file": bool(item.get("new_file")), "deleted_file": bool(item.get("deleted_file")),
            "renamed_file": bool(item.get("renamed_file")), "version_lines": version_lines,
            "diff_incomplete": partial,
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


def execute_query(settings: Settings, request: QueryRequest) -> QueryResponse:
    applications = _allowlist(settings.query_applications)
    projects = _allowlist(settings.query_projects)
    other_secrets = {
        settings.status_secret, settings.gitlab_webhook_secret,
        settings.argocd_webhook_secret, settings.teams_gateway_secret,
        settings.gitlab_token, settings.argocd_token, settings.mcp_bearer_token,
    }
    if (
        not settings.query_enabled or not settings.query_secret or not applications or not projects
        or settings.query_secret in other_secrets
    ):
        raise QueryError(503, "Query service is disabled or not fully configured")
    intent = _model_intent(settings, request.question)
    if intent == "deployed_version":
        if request.project or request.from_ref or request.to_ref:
            raise QueryError(422, "Unexpected comparison fields for deployed-version query")
        return _deployed_version(settings, _application(request, applications), projects)
    if intent == "branch_comparison":
        if request.application:
            raise QueryError(422, "Unexpected application field for comparison query")
        project = _project(request, projects)
        return _compare(settings, project, *_refs(request))
    raise QueryError(422, "Question is unsupported or ambiguous")
