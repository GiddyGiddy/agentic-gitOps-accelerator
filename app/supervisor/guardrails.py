import hmac


class PolicyViolation(ValueError):
    pass


def verify_secret(expected: str, supplied: str | None) -> None:
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise PolicyViolation("Authentication failed")


def require_write_enabled(enabled: bool) -> None:
    if not enabled:
        raise PolicyViolation("GitLab write operations are disabled")


def require_approver(actor: str, allowlist: set[str]) -> None:
    if not actor or actor not in allowlist:
        raise PolicyViolation("Approver is not authorized")
