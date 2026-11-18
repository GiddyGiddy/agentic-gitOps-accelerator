from typing import Any, Literal, TypedDict


class EventState(TypedDict, total=False):
    kind: Literal["merge_request", "tag", "job", "argocd", "approval"]
    payload: dict[str, Any]
    result: dict[str, Any]
    event_id: int
    last_event_id: int
    workflow_status: str
