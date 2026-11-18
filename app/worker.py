import argparse
import logging
import time
from pathlib import Path

import yaml
from langgraph.checkpoint.postgres import PostgresSaver
from langsmith import tracing_context
from sqlalchemy import select, text
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.agents.reconciliation_agent import application_is_healthy, parse_deploy_csv
from app.agents.routing_agent import routing_findings
from app.llm.advisory import generate_advisory
from app.llm.models import AdvisoryInput, FindingCategory
from app.mcp.client import ReadOnlyMCPClient
from app.storage.db import create_database
from app.storage.models import Approval, AuditEvent, InboxEvent, OutboxAction, Release, utcnow
from app.supervisor.graph import build_graph
from app.supervisor.guardrails import PolicyViolation, require_approver
from app.tools.argocd_client import ArgoCDClient
from app.tools.gitlab_client import GitLabClient
from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def handle_event(session: Session, item: InboxEvent, settings: Settings) -> dict:
    kind, payload = item.kind, item.payload
    result: dict = {"status": "recorded"}
    reference = kind
    if kind == "tag":
        version = payload["ref"].removeprefix("refs/tags/REL/")
        project_id = int(payload["project_id"])
        reference = version
        release = session.get(Release, version)
        if release and release.project_id != project_id:
            raise PolicyViolation("Release version already belongs to another project")
        if not release:
            session.add(Release(version=version, project_id=project_id))
        result = {"status": "release_recorded", "release": version}
    elif kind == "merge_request":
        attrs = payload["object_attributes"]
        project_id, iid = int(payload["project"]["id"]), int(attrs["iid"])
        reference = f"{project_id}!{iid}"
        if settings.mcp_mode == "mcp":
            changes = ReadOnlyMCPClient(settings).changes(project_id, iid)
        else:
            client = GitLabClient(settings.gitlab_url, settings.gitlab_token, False)
            try:
                changes = client.changes(project_id, iid)
            finally:
                client.close()
        rules = yaml.safe_load((Path(settings.config_dir) / "rules_catalog.yaml").read_text())
        findings = routing_findings(attrs["target_branch"], changes, rules)
        violations = [finding.message for finding in findings]
        if violations and settings.write_enabled:
            session.add(OutboxAction(
                event_id=item.id, action_key="mr-comment", kind="gitlab_comment",
                payload={"project_id": project_id, "iid": iid,
                         "body": "GitOps routing violations:\n" + "\n".join(violations)},
            ))
        result = {"status": "evaluated", "violations": violations}
        if settings.llm_enabled and findings:
            categories = tuple(FindingCategory(value) for value in sorted({finding.category for finding in findings}))
            result["advisory"] = generate_advisory(settings, AdvisoryInput(task="routing", categories=categories))
    elif kind == "job":
        name = payload.get("build_name", "")
        if name.startswith("deploy_release_") and payload.get("build_status") == "success":
            project_id, job_id = int(payload["project_id"]), int(payload["build_id"])
            reference = f"{project_id}:{job_id}"
            if settings.mcp_mode == "mcp":
                mcp = ReadOnlyMCPClient(settings)
                deployments = parse_deploy_csv(mcp.artifact_file(project_id, job_id, settings.deploy_csv_path))
                checks = [
                    {"application": entry.application, "chart_version": entry.chart_version,
                     "repository": entry.repository,
                     "healthy": application_is_healthy(mcp.application(entry.application), entry.chart_version)}
                    for entry in deployments
                ]
            else:
                gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token, False)
                try:
                    deployments = parse_deploy_csv(
                        gitlab.artifact_file(project_id, job_id, settings.deploy_csv_path)
                    )
                finally:
                    gitlab.close()
                argocd = ArgoCDClient(settings.argocd_url, settings.argocd_token)
                try:
                    checks = [
                        {"application": entry.application, "chart_version": entry.chart_version,
                         "repository": entry.repository,
                         "healthy": application_is_healthy(argocd.application(entry.application), entry.chart_version)}
                        for entry in deployments
                    ]
                finally:
                    argocd.close()
            result = {"status": "reconciled", "deployments": checks}
            if settings.llm_enabled:
                category = (
                    FindingCategory.RECONCILIATION_HEALTHY if all(check["healthy"] for check in checks)
                    else FindingCategory.RECONCILIATION_UNHEALTHY
                )
                result["advisory"] = generate_advisory(
                    settings, AdvisoryInput(task="reconciliation", categories=(category,))
                )
        else:
            result = {"status": "ignored", "reason": "Not a successful deploy_release job"}
    elif kind == "approval":
        require_approver(payload["actor"], settings.approvers)
        release = session.get(Release, payload["release"])
        if release is None or release.project_id != payload["project_id"]:
            raise PolicyViolation("Unknown release for project")
        waves = yaml.safe_load((Path(settings.config_dir) / "rollout_waves.yaml").read_text())["waves"]
        if not any(wave["name"] == payload["wave"] and wave["approval_required"] for wave in waves):
            raise PolicyViolation("Unknown or non-approvable wave")
        session.add(Approval(
            request_id=payload["request_id"], release=payload["release"],
            wave=payload["wave"], actor=payload["actor"],
        ))
        reference = payload["release"]
        result = {"status": "approval_recorded", "wave": payload["wave"]}
    elif kind == "argocd":
        reference = payload["application"]
    else:
        raise ValueError(f"Unknown event kind: {kind}")
    session.add(AuditEvent(kind=kind, reference=reference, details=result))
    session.flush()
    return result


def process_one(factory: sessionmaker[Session], graph, settings: Settings) -> bool:
    with factory() as session:
        with session.begin():
            earlier = aliased(InboxEvent)
            blocked = select(earlier.id).where(
                earlier.thread_id == InboxEvent.thread_id, earlier.id < InboxEvent.id,
                earlier.status.not_in(("completed", "rejected")),
            ).exists()
            item = session.scalar(
                select(InboxEvent).where(InboxEvent.status == "pending", ~blocked)
                .order_by(InboxEvent.id).with_for_update(skip_locked=True).limit(1)
            )
            if item is None:
                return False
            if session.get_bind().dialect.name == "postgresql":
                acquired = session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:thread, 0))"),
                    {"thread": item.thread_id},
                )
                if not acquired:
                    return False
            item.attempts += 1
            try:
                with session.begin_nested():
                    result = handle_event(session, item, settings)
                    with tracing_context(enabled=False):
                        graph.invoke(
                            {"kind": item.kind, "payload": item.payload,
                             "event_id": item.id, "result": result},
                            {"configurable": {"thread_id": item.thread_id}},
                            durability="sync",
                        )
                item.result = result
                item.status = "completed"
                item.completed_at = utcnow()
                item.error = None
            except Exception as exc:
                logger.exception("Event %s failed; operator retry required", item.id)
                item.status = "failed"
                item.error = f"Processing failed ({type(exc).__name__}); consult worker logs"
            return True


def dispatch_one(factory: sessionmaker[Session], settings: Settings) -> bool:
    if not settings.write_enabled:
        return False
    with factory.begin() as session:
        action = session.scalar(
            select(OutboxAction).where(OutboxAction.status == "pending")
            .order_by(OutboxAction.id).with_for_update(skip_locked=True).limit(1)
        )
        if action is None:
            return False
        action.status = "sending"
        action_id, kind, payload = action.id, action.kind, action.payload
    try:
        if kind != "gitlab_comment":
            raise ValueError(f"Unsupported outbox action: {kind}")
        client = GitLabClient(settings.gitlab_url, settings.gitlab_token, settings.write_enabled)
        try:
            client.comment(payload["project_id"], payload["iid"], payload["body"])
        finally:
            client.close()
    except Exception as exc:
        logger.exception("Outbox action %s has an uncertain outcome; manual reconciliation required", action_id)
        with factory.begin() as session:
            action = session.get(OutboxAction, action_id)
            if action is None:
                raise RuntimeError("Outbox action disappeared") from exc
            action.status = "uncertain"
            action.error = f"Delivery uncertain ({type(exc).__name__}); consult worker logs"
        return True
    with factory.begin() as session:
        action = session.get(OutboxAction, action_id)
        if action is None:
            raise RuntimeError("Outbox action disappeared after delivery")
        action.status = "completed"
    return True


def retry_event(factory: sessionmaker[Session], event_id: int) -> None:
    with factory.begin() as session:
        event = session.scalar(select(InboxEvent).where(InboxEvent.id == event_id).with_for_update())
        if event is None or event.status != "failed":
            raise ValueError("Only failed events may be retried")
        event.status = "pending"
        event.error = None


def reject_event(factory: sessionmaker[Session], event_id: int, reason: str) -> None:
    if not reason.strip():
        raise ValueError("A rejection reason is required")
    with factory.begin() as session:
        event = session.scalar(select(InboxEvent).where(InboxEvent.id == event_id).with_for_update())
        if event is None or event.status != "failed":
            raise ValueError("Only failed events may be rejected")
        event.status = "rejected"
        event.completed_at = utcnow()
        session.add(AuditEvent(kind="event_rejected", reference=str(event_id), details={"reason": reason}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the durable GitOps event worker")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--retry-event", type=int, help="Requeue one failed event after investigation")
    actions.add_argument("--reject-event", type=int, help="Reject a failed event after investigation")
    parser.add_argument("--reason", help="Audit reason when rejecting an event")
    args = parser.parse_args()
    if args.reject_event is not None and not args.reason:
        parser.error("--reject-event requires --reason")
    if args.reason and args.reject_event is None:
        parser.error("--reason requires --reject-event")
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine, factory = create_database(settings.database_url)
    try:
        if engine.dialect.name != "postgresql":
            raise ValueError("The durable worker requires PostgreSQL")
        if args.retry_event is not None:
            retry_event(factory, args.retry_event)
            return
        if args.reject_event is not None:
            reject_event(factory, args.reject_event, args.reason)
            return
        url = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with PostgresSaver.from_conn_string(url) as saver:
            graph = build_graph(saver)
            while True:
                did_work = process_one(factory, graph, settings)
                dispatched = dispatch_one(factory, settings)
                if not did_work and not dispatched:
                    time.sleep(settings.worker_poll_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
