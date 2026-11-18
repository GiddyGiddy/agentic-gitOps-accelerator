import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.storage.db import create_database
from app.storage.inbox import workflow_thread
from app.storage.models import GitLabPollState, InboxEvent, utcnow
from app.tools.gitlab_client import GitLabClient
from app.traceability import build_job_names, deploy_job_names, validate_traceability_settings
from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("GitLab merge request is missing updated_at")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("GitLab merge request updated_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _snapshot(client: GitLabClient, project: str) -> tuple[int, dict[str, dict]]:
    project_id = client.project_id(project)
    observations: dict[str, dict] = {}
    for mr in client.merge_requests(project):
        iid, target = mr.get("iid"), mr.get("target_branch")
        if type(iid) is not int or iid <= 0 or not isinstance(target, str) or not target:
            raise ValueError("GitLab returned an invalid merge request")
        key = f"mr:{iid}"
        if key in observations:
            raise ValueError("GitLab returned a merge request on multiple pages")
        observations[key] = {"updated_at": _timestamp(mr.get("updated_at")), "target_branch": target}
    for tag in client.release_tags(project) + client.hotfix_tags(project):
        name, commit = tag.get("name"), tag.get("commit")
        if not isinstance(name, str) or not name.startswith(("REL/", "HOT/")) or not isinstance(commit, dict):
            raise ValueError("GitLab returned an invalid release or hotfix tag")
        sha = commit.get("id")
        if not isinstance(sha, str) or not sha:
            raise ValueError("GitLab returned a release tag without a commit SHA")
        key = f"tag:{name}"
        if key in observations:
            raise ValueError("GitLab returned a release tag on multiple pages")
        observations[key] = {"sha": sha}
    return project_id, observations


def _event(project_id: int, key: str, observation: dict) -> dict:
    if key.startswith("mr:"):
        return {
            "kind": "merge_request",
            "payload": {
                "project": {"id": project_id},
                "object_attributes": {
                    "iid": int(key[3:]),
                    "target_branch": observation["target_branch"],
                    "updated_at": observation["updated_at"],
                },
            },
        }
    return {
        "kind": "tag",
        "payload": {"project_id": project_id, "ref": f"refs/tags/{key[4:]}", "checkout_sha": observation["sha"]},
    }


def poll_once(factory: sessionmaker[Session], settings: Settings, client: GitLabClient) -> int | None:
    source = f"{settings.gitlab_url.rstrip('/')}\n{settings.gitlab_poll_project}"
    source_key = hashlib.sha256(source.encode()).hexdigest()
    with factory.begin() as session:
        if session.get_bind().dialect.name == "postgresql":
            acquired = session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:source, 0))"),
                {"source": f"gitlab-poll:{source_key}"},
            )
            if not acquired:
                return None
        state = session.get(GitLabPollState, source_key)
        project_id, observations = _snapshot(client, settings.gitlab_poll_project)
        if state is None:
            session.add(GitLabPollState(
                source_key=source_key, project_id=project_id, generation=0, observations=observations
            ))
            return 0
        if state.project_id != project_id:
            raise ValueError("GitLab project ID changed for the configured project path")

        count = 0
        for key, observation in observations.items():
            if state.observations.get(key) == observation:
                continue
            event = _event(project_id, key, observation)
            identity = json.dumps([source_key, state.generation, key, observation], sort_keys=True)
            delivery_id = "poll-" + hashlib.sha256(identity.encode()).hexdigest()
            session.add(InboxEvent(
                source="gitlab_poll",
                delivery_id=delivery_id,
                thread_id=workflow_thread(event["kind"], event["payload"], "gitlab_poll", delivery_id),
                kind=event["kind"],
                payload=event["payload"],
            ))
            count += 1
        state.observations = observations
        state.generation += 1
        state.updated_at = utcnow()
        return count


def poll_build_jobs_once(factory: sessionmaker[Session], settings: Settings, client: GitLabClient) -> int | None:
    validate_traceability_settings(settings)
    names = build_job_names(settings)
    deploy_names = deploy_job_names(settings)
    source = (
        f"gitlab-build-jobs\n{settings.gitlab_url.rstrip('/')}\n{settings.gitlab_poll_project}\n"
        + "\n".join(sorted(names)) + "\ndeploy\n" + "\n".join(sorted(deploy_names))
    )
    source_key = hashlib.sha256(source.encode()).hexdigest()
    with factory.begin() as session:
        if session.get_bind().dialect.name == "postgresql":
            acquired = session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:source, 0))"),
                {"source": f"gitlab-poll:{source_key}"},
            )
            if not acquired:
                return None
        state = session.get(GitLabPollState, source_key)
        project_id = client.project_id(settings.gitlab_poll_project)
        jobs = client.recent_successful_jobs(settings.gitlab_poll_project)
        if len(jobs) >= 100:
            logger.warning("GitLab build job window is full; older successful jobs may require backfill")
        observations: dict[str, dict] = {}
        for job in jobs:
            if job.get("name") not in names | deploy_names:
                continue
            job_id = job.get("id")
            if type(job_id) is not int or job_id <= 0 or job.get("status") != "success":
                raise ValueError("GitLab returned an invalid successful build job")
            key = f"job:{job_id}"
            if key in observations:
                raise ValueError("GitLab returned a duplicate build job")
            observations[key] = {"id": job_id, "name": job["name"]}
        if state is None:
            session.add(GitLabPollState(source_key=source_key, project_id=project_id,
                                        generation=0, observations=observations))
            return 0
        if state.project_id != project_id:
            raise ValueError("GitLab project ID changed for the configured project path")
        count = 0
        for key, observation in observations.items():
            if key in state.observations:
                continue
            if observation["name"] in names:
                kind = "build"
                payload = {"project_id": project_id, "job_id": observation["id"]}
            else:
                kind = "job"
                payload = {"project_id": project_id, "build_id": observation["id"],
                           "build_name": observation["name"], "build_status": "success"}
            identity = json.dumps([source_key, state.generation, key], sort_keys=True)
            delivery_id = "poll-" + hashlib.sha256(identity.encode()).hexdigest()
            session.add(InboxEvent(
                source="gitlab_build_poll", delivery_id=delivery_id,
                thread_id=workflow_thread(kind, payload, "gitlab_build_poll", delivery_id),
                kind=kind, payload=payload,
            ))
            count += 1
        state.observations = observations
        state.generation += 1
        state.updated_at = utcnow()
        return count


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.gitlab_poll_project:
        raise ValueError("GITOPS_GITLAB_POLL_PROJECT is required")
    if settings.gitlab_webhook_enabled:
        raise ValueError("Disable GitLab webhook ingestion while polling is enabled")
    engine, factory = create_database(settings.database_url)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        raise ValueError("The GitLab poller requires PostgreSQL")
    try:
        client = GitLabClient(settings.gitlab_url, settings.gitlab_token)
        try:
            next_build_poll = 0.0
            if settings.gitlab_build_poll_enabled and not settings.traceability_enabled:
                logger.warning("Build polling is configured but inactive until traceability is enabled")
            while True:
                started = time.monotonic()
                try:
                    count = poll_once(factory, settings, client)
                    if count:
                        logger.info("Enqueued %d GitLab polling observations", count)
                except (httpx.HTTPError, SQLAlchemyError, ValueError, RuntimeError):
                    logger.exception("GitLab polling failed; the baseline was not advanced")
                if (
                    settings.gitlab_build_poll_enabled and settings.traceability_enabled
                    and started >= next_build_poll
                ):
                    next_build_poll = started + settings.gitlab_build_poll_seconds
                    try:
                        count = poll_build_jobs_once(factory, settings, client)
                        if count:
                            logger.info("Enqueued %d GitLab build observations", count)
                    except (httpx.HTTPError, SQLAlchemyError, ValueError, RuntimeError):
                        logger.exception("GitLab build polling failed; its baseline was not advanced")
                time.sleep(max(0, settings.gitlab_poll_seconds - (time.monotonic() - started)))
        finally:
            client.close()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
