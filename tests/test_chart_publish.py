import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.traceability import BuildProvenance


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("sh")


@pytest.fixture
def publish(tmp_path):
    if not SHELL:
        pytest.skip("POSIX shell is required")
    helm = tmp_path / "helm"
    helm.write_text(
        """#!/bin/sh
set -eu
case "$1 $2" in
  'show chart') printf 'name: helix-gitops-agents\\nversion: %s\\n' "${TEST_VERSION:-0.1.0}" ;;
  'lint deploy/helm/helix-gitops-agents') : ;;
  'package deploy/helm/helix-gitops-agents')
    shift 2
    version=$2
    destination=$4
    touch "$destination/helix-gitops-agents-$version.tgz"
    ;;
  'registry login')
    test "$(cat)" = 'test-secret'
    printf '{}' > "$HELM_REGISTRY_CONFIG"
    ;;
  push*)
    test -f "$2"
    test "$3" = 'oci://harbor.giddyitsolutions.com/charts'
    if [ "${TEST_PUSH_FAIL:-}" = true ]; then exit 1; fi
    printf '%s\\n' "${TEST_PUSH_OUTPUT:-Digest: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc}"
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    helm.chmod(0o755)
    password = tmp_path / "password"
    password.write_text("test-secret", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        PATH=str(tmp_path) + os.pathsep + env["PATH"],
        CI_COMMIT_SHA="a" * 40,
        CI_PIPELINE_ID="123",
        CI_JOB_ID="456",
        HARBOR_USERNAME="robot",
        HARBOR_PASSWORD_FILE=password.as_posix(),
    )

    def run(**overrides):
        return subprocess.run(
            [SHELL, (ROOT / "scripts" / "publish-chart.sh").as_posix()],
            cwd=tmp_path,
            env=env | overrides,
            text=True,
            capture_output=True,
        )

    return run, tmp_path


def test_publisher_creates_job_provenance_after_push(publish):
    run, workdir = publish
    result = run()
    assert result.returncode == 0, result.stderr
    assert "test-secret" not in result.stdout + result.stderr
    manifest = json.loads((workdir / "gitops-provenance.json").read_text())
    validated = BuildProvenance.model_validate(manifest)
    assert validated.commit_sha == "a" * 40
    assert validated.chart.version == validated.chart.tag == "0.1.0-ci.123.456"
    assert validated.chart.harbor_repository == "helix-gitops-agents"
    assert validated.chart.digest == "sha256:" + "c" * 64
    assert validated.fixes == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"TEST_PUSH_FAIL": "true"},
        {"TEST_PUSH_OUTPUT": "Digest: invalid"},
        {"TEST_PUSH_OUTPUT": ("Digest: sha256:" + "c" * 64 + "\n") * 2},
        {"CI_JOB_ID": "invalid"},
        {"TEST_VERSION": "0.1.0-rc.1"},
    ],
)
def test_publisher_rejects_failure_without_provenance(publish, overrides):
    run, workdir = publish
    (workdir / "gitops-provenance.json").write_text("stale", encoding="utf-8")
    result = run(**overrides)
    assert result.returncode != 0
    assert not (workdir / "gitops-provenance.json").exists()
