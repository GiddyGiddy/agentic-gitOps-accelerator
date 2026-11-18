import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoutingFinding:
    category: str
    message: str


def routing_findings(target: str, changes: list[dict[str, Any]], rules: dict[str, Any]) -> list[RoutingFinding]:
    violations: list[RoutingFinding] = []
    prefix = rules["deployment_prefix"]
    for change in changes:
        path = change.get("new_path", "")
        old_path = change.get("old_path", "")
        if not (path.startswith(prefix) or old_path.startswith(prefix)):
            continue
        diff = change.get("diff", "")
        added = "\n".join(
            line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        if re.search(r"(?m)^\s*(?:image\.tag|tag)\s*:", added):
            violations.append(RoutingFinding(
                "image_tag_in_deployment", f"{path}: image tag changes in deployment/ require review in GitOps"
            ))
        components = set(re.split(r"[/_.-]+", path.lower()))
        if target == rules["protected_target"] and components.intersection(rules["release_environments"]):
            violations.append(RoutingFinding(
                "release_target_mismatch", f"{path}: release environment changes must target release/VN*"
            ))
        if target.startswith("release/") and not re.fullmatch(rules["release_branch_pattern"], target):
            violations.append(RoutingFinding("invalid_release_branch", f"{path}: invalid release branch {target}"))
    return violations


def validate_changes(target: str, changes: list[dict[str, Any]], rules: dict[str, Any]) -> list[str]:
    return [finding.message for finding in routing_findings(target, changes, rules)]
