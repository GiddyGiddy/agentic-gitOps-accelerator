from pathlib import Path

import pytest

from app.agents.reconciliation_agent import application_is_healthy, parse_deploy_csv


def test_parse_and_verify_chart_revision():
    deployments = parse_deploy_csv(Path("tests/fixtures/sample_deploy_output.csv").read_bytes())
    assert deployments[0].application == "vn-base"
    app = {
        "spec": {"source": {"targetRevision": "260901.4"}},
        "status": {"sync": {"status": "Synced", "revision": "260901.4"}, "health": {"status": "Healthy"}},
    }
    assert application_is_healthy(app, deployments[0].chart_version)
    app["status"]["sync"]["revision"] = "260901.3"
    assert not application_is_healthy(app, deployments[0].chart_version)


def test_reject_incomplete_csv():
    with pytest.raises(ValueError):
        parse_deploy_csv(b"application,chart_version,repository\napp,,repo\n")
