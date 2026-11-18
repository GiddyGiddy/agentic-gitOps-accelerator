from typing import Any

from app.supervisor.guardrails import PolicyViolation


def validate_promotion(
    wave: dict[str, Any], jobs: list[dict[str, Any]], previous_wave_healthy: bool, approved: bool
) -> list[int]:
    if not previous_wave_healthy:
        raise PolicyViolation("Previous wave has not been verified healthy")
    if wave["approval_required"] and not approved:
        raise PolicyViolation("Wave requires an authorized approval")
    result = []
    for name in wave["jobs"]:
        matches = [job for job in jobs if job.get("name") == name and job.get("status") == "manual"]
        if len(matches) != 1 or not isinstance(matches[0].get("id"), int):
            raise PolicyViolation(f"Expected exactly one manual job for {name}")
        result.append(matches[0]["id"])
    return result
