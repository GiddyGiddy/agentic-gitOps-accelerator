from typing import Any


def normalize_argocd_event(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload.get("application"), str) or not payload["application"]:
        raise ValueError("application is required")
    return {"kind": "argocd", "payload": payload}
