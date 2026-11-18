import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import InboxEvent


def workflow_thread(kind: str, payload: dict[str, Any], source: str, delivery_id: str) -> str:
    if kind == "build":
        project, job = int(payload["project_id"]), int(payload["job_id"])
        if project <= 0 or job <= 0:
            raise ValueError("Invalid build project ID or job ID")
        digest = hashlib.sha256(f"{project}:{job}".encode()).hexdigest()
        return f"build:{digest}"
    if kind == "tag":
        project = int(payload["project_id"])
        ref = payload["ref"]
        if not isinstance(ref, str) or not ref.startswith(("refs/tags/REL/", "refs/tags/HOT/")):
            raise ValueError("Invalid release or hotfix tag")
        version = ref.split("/", 3)[-1]
    elif kind == "approval":
        project = int(payload["project_id"])
        version = payload["release"]
    else:
        digest = hashlib.sha256(f"{source}:{delivery_id}".encode()).hexdigest()
        return f"event:{digest}"
    if project <= 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version):
        raise ValueError("Invalid project ID or release version")
    digest = hashlib.sha256(f"{project}:{version}".encode()).hexdigest()
    return f"release:{digest}"


def enqueue(
    factory: sessionmaker[Session], source: str, delivery_id: str, event: dict[str, Any]
) -> tuple[InboxEvent, bool]:
    if not delivery_id or len(delivery_id) > 128:
        raise ValueError("A delivery ID of at most 128 characters is required")
    thread_id = workflow_thread(event["kind"], event["payload"], source, delivery_id)
    with factory() as session:
        item = InboxEvent(
            source=source, delivery_id=delivery_id, kind=event["kind"],
            payload=event["payload"], thread_id=thread_id,
        )
        session.add(item)
        try:
            session.commit()
            return item, False
        except IntegrityError:
            session.rollback()
            previous = session.scalar(
                select(InboxEvent).where(
                    InboxEvent.source == source, InboxEvent.delivery_id == delivery_id
                )
            )
            if previous is None:
                raise
            if previous.kind != event["kind"] or json.dumps(
                previous.payload, sort_keys=True
            ) != json.dumps(event["payload"], sort_keys=True):
                raise ValueError("Delivery ID already used for a different event")
            return previous, True
