from typing import Any


def normalize_gitlab_event(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_name == "Merge Request Hook":
        return {"kind": "merge_request", "payload": payload}
    if event_name == "Tag Push Hook":
        ref = payload.get("ref", "")
        if isinstance(ref, str) and ref.startswith("refs/tags/REL/") and payload.get("checkout_sha"):
            return {"kind": "tag", "payload": payload}
    if event_name == "Job Hook":
        return {"kind": "job", "payload": payload}
    raise ValueError("Unsupported GitLab event")
