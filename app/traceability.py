import argparse
import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.providers import validate_url
from app.storage.db import create_database
from app.storage.inbox import enqueue
from app.storage.models import TraceBuild, TraceFix
from app.tools.gitlab_client import GitLabClient
from app.tools.harbor_client import HarborClient
from config.settings import Settings, get_settings


SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
CHART_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


class ChartProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    harbor_project: str = Field(min_length=1, max_length=256)
    harbor_repository: str = Field(min_length=1, max_length=512)
    tag: str = Field(min_length=1, max_length=256)
    digest: str = Field(min_length=1, max_length=71)


class FixProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    commit_sha: str
    patch_paths: list[str] = Field(default_factory=list, max_length=20)


class ImageProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository: str = Field(min_length=3, max_length=512)
    digest: str = Field(min_length=71, max_length=71)


class EnvironmentProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    application: str = Field(min_length=1, max_length=200)
    config_project: str = Field(min_length=3, max_length=300)
    config_revision: str = Field(min_length=40, max_length=64)
    images: list[ImageProvenance] = Field(min_length=1, max_length=50)


class BuildProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1, 2]
    commit_sha: str
    chart: ChartProvenance
    fixes: list[FixProvenance] = Field(max_length=50)
    environments: list[EnvironmentProvenance] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def validate_version(self):
        if self.schema_version == 1 and self.environments is not None:
            raise ValueError("Schema version 1 cannot contain environments")
        if self.schema_version == 2 and not self.environments:
            raise ValueError("Schema version 2 requires environments")
        return self


def build_job_names(settings: Settings) -> set[str]:
    return {name.strip() for name in settings.traceability_build_jobs.split(",") if name.strip()}


def deploy_job_names(settings: Settings) -> set[str]:
    return {name.strip() for name in settings.traceability_deploy_jobs.split(",") if name.strip()}


def validate_traceability_settings(settings: Settings) -> tuple[set[str], tuple[str, ...]]:
    jobs = build_job_names(settings)
    deploy_jobs = deploy_job_names(settings)
    prefixes = tuple(part.strip().upper() for part in settings.traceability_jira_prefixes.split(",") if part.strip())
    if (
        not settings.traceability_enabled or not settings.gitlab_poll_project or not jobs
        or not settings.traceability_harbor_project or not settings.traceability_harbor_repository
        or not settings.traceability_manifest_path or not prefixes
        or any(not re.fullmatch(r"[A-Z][A-Z0-9]{1,14}", prefix) for prefix in prefixes)
        or jobs & deploy_jobs or any(not name.startswith("deploy_release_") for name in deploy_jobs)
    ):
        raise ValueError("Traceability is disabled or its provenance settings are incomplete")
    validate_url(settings.gitlab_url, azure=False)
    validate_url(settings.harbor_url, azure=False)
    directory = settings.traceability_patch_directory
    if not directory or directory.startswith("/") or any(part in ("", ".", "..") for part in directory.split("/")):
        raise ValueError("Invalid traceability patch directory")
    return jobs, prefixes


def jira_references(text: str, prefixes: tuple[str, ...]) -> set[str]:
    pattern = r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(prefix) for prefix in prefixes) + r")-?\d{1,10}(?![A-Za-z0-9])"
    return {match.group().upper() for match in re.finditer(pattern, text, re.IGNORECASE)}


def index_build(session: Session, settings: Settings, project_id: int, job_id: int) -> TraceBuild:
    jobs, prefixes = validate_traceability_settings(settings)
    if project_id <= 0 or job_id <= 0:
        raise ValueError("Invalid build project or job ID")
    gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        if gitlab.project_id(settings.gitlab_poll_project) != project_id:
            raise ValueError("Build project does not match configured project")
        job = gitlab.job(project_id, job_id)
        commit = job.get("commit")
        pipeline = job.get("pipeline")
        ref = job.get("ref")
        if (
            job.get("id") != job_id or job.get("status") != "success" or job.get("name") not in jobs
            or not isinstance(commit, dict) or not isinstance(commit.get("id"), str)
            or not SHA.fullmatch(commit["id"]) or not isinstance(pipeline, dict)
            or type(pipeline.get("id")) is not int or pipeline["id"] <= 0
            or pipeline.get("sha", commit["id"]) != commit["id"]
            or not isinstance(ref, str) or not ref or len(ref) > 256
        ):
            raise ValueError("Build job has invalid or unauthorized provenance")
        raw = gitlab.artifact_file(project_id, job_id, settings.traceability_manifest_path, max_bytes=65536)
        manifest = BuildProvenance.model_validate_json(raw)
        chart = manifest.chart
        if (
            manifest.commit_sha != commit["id"] or not SHA.fullmatch(manifest.commit_sha)
            or not CHART_VERSION.fullmatch(chart.version) or not DIGEST.fullmatch(chart.digest)
            or not TAG.fullmatch(chart.tag)
            or chart.harbor_project != settings.traceability_harbor_project
            or chart.harbor_repository != settings.traceability_harbor_repository
            or sum(len(fix.patch_paths) for fix in manifest.fixes) > 50
        ):
            raise ValueError("Manifest conflicts with job or authorized chart registry")
        references: set[tuple[str, str, str]] = set()
        seen: set[str] = set()
        for fix in manifest.fixes:
            sha = fix.commit_sha
            if not SHA.fullmatch(sha) or sha in seen:
                raise ValueError("Manifest has an invalid or duplicate fix commit")
            seen.add(sha)
            if gitlab.merge_base(project_id, sha, manifest.commit_sha) != sha:
                raise ValueError("A fix commit is not an ancestor of the build commit")
            fix_commit = gitlab.commit(project_id, sha)
            if (
                fix_commit.get("id") != sha or not isinstance(fix_commit.get("message"), str)
                or len(fix_commit["message"]) > 100000
            ):
                raise ValueError("GitLab returned an invalid fix commit")
            for jira_id in jira_references(fix_commit["message"], prefixes):
                references.add((jira_id, sha, ""))
            for path in fix.patch_paths:
                if (
                    not isinstance(path, str) or len(path) > 512
                    or not path.startswith(settings.traceability_patch_directory.rstrip("/") + "/")
                    or any(part in ("", ".", "..") for part in path.split("/"))
                ):
                    raise ValueError("Manifest contains an invalid patch path")
                gitlab.repository_file(project_id, path, manifest.commit_sha)
                for jira_id in jira_references(path, prefixes):
                    references.add((jira_id, sha, path))
    finally:
        gitlab.close()

    harbor = HarborClient(settings.harbor_url, settings.harbor_username, settings.harbor_password)
    try:
        artifact = harbor.artifact(chart.harbor_project, chart.harbor_repository, chart.tag)
    finally:
        harbor.close()
    if not isinstance(artifact, dict) or artifact.get("digest") != chart.digest:
        raise ValueError("Harbor tag does not resolve to the manifest digest")

    values = {
        "project_path": settings.gitlab_poll_project, "pipeline_id": pipeline["id"],
        "job_name": job["name"], "commit_sha": manifest.commit_sha, "ref": ref,
        "chart_name": chart.name, "chart_version": chart.version, "harbor_url": settings.harbor_url.rstrip("/"),
        "harbor_project": chart.harbor_project, "harbor_repository": chart.harbor_repository,
        "harbor_tag": chart.tag, "digest": chart.digest, "manifest_path": settings.traceability_manifest_path,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }
    source = settings.gitlab_url.rstrip("/")
    build = session.scalar(select(TraceBuild).where(
        TraceBuild.source == source, TraceBuild.project_id == project_id, TraceBuild.job_id == job_id,
    ))
    if build:
        if any(getattr(build, key) != value for key, value in values.items()):
            raise ValueError("Existing build provenance conflicts with the current observation")
    else:
        build = TraceBuild(source=source, project_id=project_id, job_id=job_id, **values)
        session.add(build)
        session.flush()
    existing = set(session.execute(select(TraceFix.jira_id, TraceFix.commit_sha, TraceFix.evidence_path).where(
        TraceFix.build_id == build.id,
    )).all())
    for jira_id, sha, path in references - existing:
        session.add(TraceFix(build_id=build.id, jira_id=jira_id, commit_sha=sha, evidence_path=path))
    session.flush()
    return build


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue explicit GitLab build jobs for traceability backfill")
    parser.add_argument("--job-id", action="append", type=int, required=True)
    args = parser.parse_args()
    settings = get_settings()
    validate_traceability_settings(settings)
    if len(args.job_id) > 100 or any(job <= 0 for job in args.job_id):
        parser.error("Provide at most 100 positive job IDs")
    client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
    try:
        project_id = client.project_id(settings.gitlab_poll_project)
    finally:
        client.close()
    engine, factory = create_database(settings.database_url)
    try:
        for job_id in sorted(set(args.job_id)):
            source = settings.gitlab_url.rstrip("/")
            identity = hashlib.sha256(f"{source}\n{project_id}\n{job_id}".encode()).hexdigest()
            item, duplicate = enqueue(factory, "trace_backfill", identity, {
                "kind": "build", "payload": {"project_id": project_id, "job_id": job_id},
            })
            print(f"job={job_id} event={item.id} duplicate={duplicate} status={item.status}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
