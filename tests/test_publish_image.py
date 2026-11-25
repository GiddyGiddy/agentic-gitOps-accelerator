import io
import json
import os
import zipfile

import httpx
import pytest

from scripts import publish_image as pub

REPO = "owner/repo"
COMMIT = "b" * 40
DIGEST = f"sha256:{'a' * 64}"
REGISTRY = "example.azurecr.io"


def evidence_zip(run_id=11, attempt=1, digest=DIGEST):
    text = (
        f"Image: {REGISTRY}/agentic-gitops-staging@{digest}\n"
        f"Commit: {COMMIT}\nRun ID: {run_id}\nRun attempt: {attempt}\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("image-digest.txt", text)
    return buffer.getvalue()


def run(run_id=11, attempt=1, status="completed", **overrides):
    value = {
        "id": run_id, "run_attempt": attempt, "status": status, "head_sha": COMMIT, "event": "push",
        "head_branch": "main", "head_repository": {"full_name": REPO}, "repository": {"full_name": REPO},
    }
    return value | overrides


def job(conclusion="success", status="completed", name=pub.PUBLISH_JOB):
    return {"name": name, "status": status, "conclusion": conclusion}


class FakeGitHub:
    """Routes the GitHub API calls the publisher makes."""

    def __init__(self, runs, jobs, artifacts=None, archive=None, redirect="https://x.blob.core.windows.net/a?sig=secret"):
        self.runs, self.jobs, self.artifacts = runs, jobs, artifacts or {}
        self.archive, self.redirect, self.requests = archive, redirect, []
        self.harbor_ready = lambda: True

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/workflows/ci.yml/runs"):
            return httpx.Response(200, json={"workflow_runs": self.runs() if callable(self.runs) else self.runs})
        if "/attempts/" in path and path.endswith("/jobs"):
            run_id, attempt = int(path.split("/runs/")[1].split("/")[0]), int(path.split("/attempts/")[1].split("/")[0])
            return httpx.Response(200, json={"jobs": self.jobs[(run_id, attempt)]})
        if path.endswith("/artifacts"):
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return httpx.Response(200, json={"artifacts": self.artifacts.get(run_id, [])})
        if path.endswith("/zip"):
            return httpx.Response(302, headers={"location": self.redirect})
        if request.url.host.endswith("blob.core.windows.net"):
            return httpx.Response(200, content=self.archive)
        if path == "/api/v2.0/ping":
            return httpx.Response(200 if self.harbor_ready() else 503, text="Pong")
        return httpx.Response(404)


def artifact(run_id=11, attempt=1, **overrides):
    return {"id": 5, "name": f"image-digest-{COMMIT}-{run_id}-{attempt}", "expired": False,
            "workflow_run": {"id": run_id, "head_sha": COMMIT}} | overrides


def client(fake, token="t"):
    return pub.GitHub(token, transport=httpx.MockTransport(fake))


def test_finds_single_successful_publication_and_ignores_other_runs():
    fake = FakeGitHub(
        [run(), run(12, head_branch="dev"), run(13, event="pull_request"), run(14, head_repository={"full_name": "fork/repo"})],
        {(11, 1): [job(name="Tests"), job()]},
    )
    candidates, unfinished, matched = pub.find_candidates(client(fake), REPO, COMMIT)
    assert candidates == [pub.Candidate(11, 1)] and not unfinished and matched == 1


def test_failed_first_attempt_then_successful_rerun_is_one_candidate():
    fake = FakeGitHub([run(attempt=2)], {(11, 1): [job("failure")], (11, 2): [job()]})
    assert pub.find_candidates(client(fake), REPO, COMMIT)[0] == [pub.Candidate(11, 2)]


def test_waits_for_unfinished_run_then_succeeds():
    state = {"calls": 0}

    def runs():
        state["calls"] += 1
        return [run(status="in_progress" if state["calls"] < 3 else "completed")]

    jobs = {(11, 1): [job(status="in_progress", conclusion=None)]}
    fake = FakeGitHub(runs, jobs)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if state["calls"] >= 2:
            jobs[(11, 1)] = [job()]

    assert pub.wait_for_candidate(client(fake), REPO, COMMIT, 100, 5, sleep=sleep) == pub.Candidate(11, 1)
    assert sleeps == [5, 5]


def test_waits_while_github_has_not_started_and_times_out():
    now = {"t": 0}
    fake = FakeGitHub([], {})
    with pytest.raises(pub.PublishError, match="Timed out"):
        pub.wait_for_candidate(client(fake), REPO, COMMIT, 20, 10, sleep=lambda s: now.update(t=now["t"] + s), clock=lambda: now["t"])


def test_fails_fast_when_workflow_finished_without_success():
    fake = FakeGitHub([run()], {(11, 1): [job("failure")]})
    with pytest.raises(pub.PublishError, match="without a successful"):
        pub.wait_for_candidate(client(fake), REPO, COMMIT, 100, 1, sleep=lambda s: pytest.fail("must not wait"))


def test_multiple_successful_publications_are_ambiguous():
    fake = FakeGitHub([run(11), run(12)], {(11, 1): [job()], (12, 1): [job()]})
    with pytest.raises(pub.PublishError, match="More than one"):
        pub.wait_for_candidate(client(fake), REPO, COMMIT, 100, 1)


def test_artifact_download_does_not_forward_credentials_and_validates_content():
    fake = FakeGitHub([], {}, {11: [artifact()]}, evidence_zip())
    evidence = pub.fetch_evidence(client(fake), REPO, COMMIT, pub.Candidate(11, 1), REGISTRY)
    assert evidence.digest == DIGEST and evidence.repository == f"{REGISTRY}/agentic-gitops-staging"
    api, blob = fake.requests[-2], fake.requests[-1]
    assert api.headers["authorization"] == "Bearer t"
    assert "authorization" not in blob.headers


@pytest.mark.parametrize(
    "artifacts",
    [[], [artifact(expired=True)], [artifact(), artifact(id=6)], [artifact(workflow_run={"id": 11, "head_sha": "c" * 40})]],
)
def test_requires_exactly_one_matching_unexpired_artifact(artifacts):
    fake = FakeGitHub([], {}, {11: artifacts}, evidence_zip())
    with pytest.raises(pub.PublishError, match="exactly one"):
        pub.fetch_evidence(client(fake), REPO, COMMIT, pub.Candidate(11, 1), REGISTRY)


@pytest.mark.parametrize(
    "redirect", ["http://x.blob.core.windows.net/a", "https://evil.example.com/a", "https://x.blob.core.windows.net.evil.com/a"]
)
def test_rejects_unexpected_redirects(redirect):
    fake = FakeGitHub([], {}, {11: [artifact()]}, evidence_zip(), redirect=redirect)
    with pytest.raises(pub.PublishError, match="unexpected location"):
        pub.fetch_evidence(client(fake), REPO, COMMIT, pub.Candidate(11, 1), REGISTRY)
    assert all(request.url.host != "evil.example.com" for request in fake.requests)


def test_oversized_download_and_mismatched_evidence_are_rejected():
    big = FakeGitHub([], {}, {11: [artifact()]}, b"x" * (pub.MAX_ARCHIVE_BYTES + 1))
    with pytest.raises(pub.PublishError, match="size limit"):
        pub.fetch_evidence(client(big), REPO, COMMIT, pub.Candidate(11, 1), REGISTRY)
    wrong = FakeGitHub([], {}, {11: [artifact()]}, evidence_zip(run_id=99))
    with pytest.raises(pub.PublishError, match="does not match"):
        pub.fetch_evidence(client(wrong), REPO, COMMIT, pub.Candidate(11, 1), REGISTRY)


def test_artifact_requires_a_token():
    fake = FakeGitHub([], {}, {11: [artifact()]}, evidence_zip())
    with pytest.raises(pub.PublishError, match="token is required"):
        pub.fetch_evidence(client(fake, token=None), REPO, COMMIT, pub.Candidate(11, 1), REGISTRY)


def test_copy_uses_the_verified_digest_and_verifies_the_destination():
    manifest = b'{"schemaVersion":2}'
    import hashlib

    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    calls = []

    def skopeo(args):
        calls.append(args)
        return manifest if args[0] == "inspect" else b""

    assert pub.copy_and_verify("src/repo", digest, "harbor/p/r", "git-abc", "/src.json", "/dest.json", skopeo) == digest
    copy = calls[0]
    assert copy[:3] == ["copy", "--all", "--preserve-digests"] and f"docker://src/repo@{digest}" in copy
    assert copy[-1] == "docker://harbor/p/r:git-abc" and "--src-authfile" in copy and "--dest-authfile" in copy
    with pytest.raises(pub.PublishError, match="does not match"):
        pub.copy_and_verify("src/repo", DIGEST, "harbor/p/r", "t", "/s", "/d", skopeo)


def test_main_end_to_end_writes_provenance_without_secrets(tmp_path, monkeypatch):
    import hashlib

    manifest = b'{"schemaVersion":2,"x":1}'
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    for name, content in (("gh", "ghp_secrettoken"), ("acr", "{}"), ("harborpw", "robotpass")):
        (tmp_path / name).write_text(content)
    env = {
        "GITHUB_REPOSITORY": REPO, "CI_COMMIT_SHA": COMMIT, "ACR_REGISTRY": REGISTRY,
        "HARBOR_HOST": "harbor.example.com", "HARBOR_IMAGE_REPOSITORY": "charts/agentic-gitops-staging",
        "HARBOR_USERNAME": "robot$charts+publisher", "HARBOR_PASSWORD_FILE": str(tmp_path / "harborpw"),
        "GITHUB_TOKEN_FILE": str(tmp_path / "gh"), "ACR_AUTHFILE": str(tmp_path / "acr"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    fake = FakeGitHub([run()], {(11, 1): [job()]}, {11: [artifact()]}, evidence_zip(digest=digest))
    seen = {}

    def skopeo(args):
        if args[0] == "copy":
            authfile = args[args.index("--dest-authfile") + 1]
            seen["auth"] = json.loads(open(authfile).read())
            seen["mode"] = os.stat(authfile).st_mode & 0o777
            seen["path"] = authfile
        return manifest if args[0] == "inspect" else b""

    pub.main(skopeo=skopeo, transport=httpx.MockTransport(fake), sleep=lambda s: None)
    provenance = json.loads((tmp_path / "image-provenance.json").read_text())
    assert provenance["image"]["digest"] == digest and provenance["github"] == {"repository": REPO, "run_id": 11, "run_attempt": 1}
    serialized = json.dumps(provenance)
    assert "ghp_secrettoken" not in serialized and "robotpass" not in serialized
    assert list(seen["auth"]["auths"]) == ["harbor.example.com"]
    assert not os.path.exists(seen["path"])
    if os.name != "nt":
        assert seen["mode"] == 0o600


def test_waits_for_harbor_readiness_then_times_out():
    fake = FakeGitHub([], {})
    answers = iter([False, False, True])
    fake.harbor_ready = lambda: next(answers)
    sleeps = []
    pub.wait_for_harbor("harbor.example.com", 100, 15, transport=httpx.MockTransport(fake), sleep=sleeps.append)
    assert sleeps == [15, 15]
    never = FakeGitHub([], {})
    never.harbor_ready = lambda: False
    now = {"t": 0}
    with pytest.raises(pub.PublishError, match="Harbor did not become ready"):
        pub.wait_for_harbor("harbor.example.com", 30, 15, transport=httpx.MockTransport(never),
                            sleep=lambda s: now.update(t=now["t"] + s), clock=lambda: now["t"])


def test_skopeo_failures_show_sanitized_stderr(monkeypatch):
    class Result:
        returncode = 1
        stdout = b""
        stderr = b'time=1 level=fatal msg="copying: unauthorized: authentication required for hunter2"\n'

    monkeypatch.setattr(pub.subprocess, "run", lambda *a, **k: Result())
    monkeypatch.setattr(pub, "_REDACTIONS", ["hunter2"])
    with pytest.raises(pub.PublishError) as caught:
        pub.run_skopeo(["copy", "x"])
    message = str(caught.value)
    assert "unauthorized" in message and "hunter2" not in message and "<redacted>" in message


def test_missing_and_invalid_settings_are_reported_differently(monkeypatch):
    monkeypatch.delenv("HARBOR_USERNAME", raising=False)
    with pytest.raises(pub.PublishError, match="HARBOR_USERNAME is not set"):
        pub._env("HARBOR_USERNAME", pub._HOST)
    monkeypatch.setenv("HARBOR_USERNAME", "BAD VALUE")
    with pytest.raises(pub.PublishError, match="HARBOR_USERNAME has an invalid format"):
        pub._env("HARBOR_USERNAME", pub._HOST)


def test_main_refuses_debug_tracing(monkeypatch):
    monkeypatch.setenv("CI_DEBUG_TRACE", "true")
    with pytest.raises(pub.PublishError, match="debug tracing"):
        pub.main()


def test_gitlab_publish_job_is_opt_in_protected_and_uses_a_dedicated_tag():
    from pathlib import Path

    import yaml

    job_definition = yaml.safe_load((Path(__file__).resolve().parents[1] / ".gitlab-ci.yml").read_text())["publish_image"]
    assert job_definition["tags"] == ["harbor-image-publisher"]
    enable, fallback = job_definition["rules"]
    assert fallback == {"when": "never"}
    for required in ('$IMAGE_PUBLISH_ENABLED == "true"', '$CI_COMMIT_REF_PROTECTED == "true"',
                     '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH', '$CI_PIPELINE_SOURCE == "push"',
                     '$CI_DEBUG_TRACE != "true"', "@sha256:"):
        assert required in enable["if"]
    assert job_definition["script"] == ["python scripts/publish_image.py"]
