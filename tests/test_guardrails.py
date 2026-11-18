import pytest

from app.agents.promotion_agent import validate_promotion
from app.supervisor.guardrails import PolicyViolation, require_write_enabled, verify_secret
from app.tools.gitlab_client import GitLabClient


def test_empty_secrets_and_writes_fail_closed():
    with pytest.raises(PolicyViolation):
        verify_secret("", "")
    with pytest.raises(PolicyViolation):
        require_write_enabled(False)
    client = GitLabClient("https://gitlab.example", "token")
    try:
        with pytest.raises(PolicyViolation):
            client.play_job(123, 456)
    finally:
        client.close()


def test_promotion_requires_health_approval_and_unique_manual_jobs():
    wave = {"approval_required": True, "jobs": ["deploy_release_emea_vbl"]}
    jobs = [{"name": "deploy_release_emea_vbl", "status": "manual", "id": 7}]
    with pytest.raises(PolicyViolation):
        validate_promotion(wave, jobs, True, False)
    with pytest.raises(PolicyViolation):
        validate_promotion(wave, jobs, False, True)
    with pytest.raises(PolicyViolation):
        validate_promotion(wave, jobs + jobs, True, True)
    assert validate_promotion(wave, jobs, True, True) == [7]
