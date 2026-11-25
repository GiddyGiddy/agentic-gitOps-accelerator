import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.image_digest_evidence import validate_image_digest_artifact


@pytest.fixture
def publish_steps():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["publish"]["steps"]


@pytest.fixture
def bash():
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    executable = shutil.which("bash")
    if not executable:
        pytest.skip("Bash is required to exercise the GitHub Actions script")
    return executable


def run_digest_script(bash, publish_steps, tmp_path, **overrides):
    step = next(step for step in publish_steps if step.get("name") == "Record published digest")
    env = os.environ | {
        "REGISTRY": "example.azurecr.io",
        "DIGEST": f"sha256:{'a' * 64}",
        "COMMIT_SHA": "b" * 40,
        "RUN_ID": "123",
        "RUN_ATTEMPT": "2",
        "GITHUB_STEP_SUMMARY": "summary.txt",
    } | overrides
    return subprocess.run(
        [bash, "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def test_digest_artifact_identifies_run_attempt(bash, publish_steps, tmp_path):
    result = run_digest_script(bash, publish_steps, tmp_path)
    assert result.returncode == 0, result.stderr
    expected = (
        f"Image: example.azurecr.io/agentic-gitops-staging@sha256:{'a' * 64}\n"
        f"Commit: {'b' * 40}\nRun ID: 123\nRun attempt: 2\n"
    )
    assert (tmp_path / "image-digest.txt").read_text() == expected
    assert (tmp_path / "summary.txt").read_text() == expected
    upload = next(step for step in publish_steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "image-digest-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.write(tmp_path / "image-digest.txt", "image-digest.txt")
    parsed = validate_image_digest_artifact(
        archive.getvalue(), registry="example.azurecr.io", commit="b" * 40, run_id=123, attempt=2
    )
    assert parsed.digest == f"sha256:{'a' * 64}"


@pytest.mark.parametrize(
    "overrides",
    [{"DIGEST": "sha256:bad"}, {"RUN_ID": "bad"}, {"RUN_ATTEMPT": "2x"}],
)
def test_digest_artifact_rejects_invalid_identifiers(bash, publish_steps, tmp_path, overrides):
    result = run_digest_script(bash, publish_steps, tmp_path, **overrides)
    assert result.returncode != 0
    assert not (tmp_path / "image-digest.txt").exists()
