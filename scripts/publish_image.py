"""Verify GitHub image evidence, then copy the image from ACR to Harbor by digest.

Runs in a protected GitLab job. Run and job metadata come from the public GitHub
API; only the artifact download needs a GitHub token. Registry credentials are
supplied as skopeo auth files, never on a command line.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

try:
    from scripts.image_digest_evidence import MAX_ARCHIVE_BYTES, validate_image_digest_artifact
except ImportError:  # executed as `python scripts/publish_image.py`
    from image_digest_evidence import MAX_ARCHIVE_BYTES, validate_image_digest_artifact

GITHUB_API = "https://api.github.com"
WORKFLOW_FILE = "ci.yml"
PUBLISH_JOB = "Publish image to ACR"
DOWNLOAD_HOST_SUFFIXES = (".githubusercontent.com", ".blob.core.windows.net")
_COMMIT = re.compile(r"[a-f0-9]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_HOST = re.compile(r"[a-z0-9.-]+")
_PATH = re.compile(r"[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)+")


class PublishError(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    run_id: int
    attempt: int


class GitHub:
    def __init__(self, token=None, transport=None):
        self.token = token
        self.client = httpx.Client(timeout=30, follow_redirects=False, transport=transport)

    def _headers(self, authenticated):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authenticated and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def json(self, path, **params):
        response = self.client.get(f"{GITHUB_API}{path}", params=params, headers=self._headers(True))
        if response.status_code != 200:
            raise PublishError(f"GitHub returned HTTP {response.status_code} for {path.split('?')[0]}")
        return response.json()

    def download_artifact(self, repo, artifact_id):
        if not self.token:
            raise PublishError("A GitHub token is required to download the artifact")
        response = self.client.get(
            f"{GITHUB_API}/repos/{repo}/actions/artifacts/{artifact_id}/zip", headers=self._headers(True)
        )
        if response.status_code != 302:
            raise PublishError(f"Artifact download did not redirect (HTTP {response.status_code})")
        location = urlparse(response.headers.get("location", ""))
        if location.scheme != "https" or not (location.hostname or "").endswith(DOWNLOAD_HOST_SUFFIXES):
            raise PublishError("Artifact download redirected to an unexpected location")
        # The redirect URL is a short-lived secret and the GitHub credential must not follow it.
        data = bytearray()
        with self.client.stream("GET", response.headers["location"], headers={"Accept": "*/*"}) as download:
            if download.status_code != 200:
                raise PublishError(f"Artifact content download failed (HTTP {download.status_code})")
            for chunk in download.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_ARCHIVE_BYTES:
                    raise PublishError("Artifact exceeds the size limit")
        return bytes(data)


def find_candidates(github, repo, commit):
    """Return (successful publish attempts, whether any attempt is unfinished, matching run count)."""
    runs = github.json(
        f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs",
        head_sha=commit, event="push", branch="main", per_page=100,
    )["workflow_runs"]
    candidates, unfinished, matched = [], False, 0
    for run in runs:
        if (
            run.get("head_sha") != commit
            or run.get("event") != "push"
            or run.get("head_branch") != "main"
            or (run.get("head_repository") or {}).get("full_name") != repo
            or (run.get("repository") or {}).get("full_name") != repo
        ):
            continue
        matched += 1
        for attempt in range(1, int(run["run_attempt"]) + 1):
            jobs = github.json(f"/repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs", per_page=100)["jobs"]
            publish = [job for job in jobs if job.get("name") == PUBLISH_JOB]
            if any(job.get("status") != "completed" for job in publish) or (not publish and run.get("status") != "completed"):
                unfinished = True
            elif any(job.get("conclusion") == "success" for job in publish):
                candidates.append(Candidate(int(run["id"]), attempt))
    return candidates, unfinished, matched


def wait_for_candidate(github, repo, commit, timeout_seconds, poll_seconds, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout_seconds
    while True:
        candidates, unfinished, matched = find_candidates(github, repo, commit)
        if len(candidates) > 1:
            raise PublishError("More than one successful image publication exists for this commit; resolve manually")
        if len(candidates) == 1 and not unfinished:
            return candidates[0]
        if matched and not unfinished and not candidates:
            raise PublishError("The GitHub workflow for this commit completed without a successful image publication")
        if clock() >= deadline:
            raise PublishError("Timed out waiting for the GitHub image publication")
        sleep(poll_seconds)


def fetch_evidence(github, repo, commit, candidate, registry):
    name = f"image-digest-{commit}-{candidate.run_id}-{candidate.attempt}"
    artifacts = github.json(f"/repos/{repo}/actions/runs/{candidate.run_id}/artifacts", name=name, per_page=100)["artifacts"]
    matches = [
        a for a in artifacts
        if a.get("name") == name and not a.get("expired") and (a.get("workflow_run") or {}).get("head_sha") == commit
        and (a.get("workflow_run") or {}).get("id") == candidate.run_id
    ]
    if len(matches) != 1:
        raise PublishError(f"Expected exactly one unexpired artifact {name}, found {len(matches)}")
    archive = github.download_artifact(repo, matches[0]["id"])
    try:
        return validate_image_digest_artifact(
            archive, registry=registry, commit=commit, run_id=candidate.run_id, attempt=candidate.attempt
        )
    except ValueError as error:
        raise PublishError(str(error)) from error


def run_skopeo(args):
    result = subprocess.run(["skopeo", *args], capture_output=True, check=False)
    if result.returncode != 0:
        raise PublishError(f"skopeo {args[0]} failed with exit code {result.returncode}")
    return result.stdout


def wait_for_harbor(host, timeout_seconds, poll_seconds, transport=None, sleep=time.sleep, clock=time.monotonic):
    """AKS reports Running before Harbor's pods are ready, so wait for its unauthenticated ping."""
    deadline = clock() + timeout_seconds
    with httpx.Client(timeout=10, follow_redirects=False, transport=transport) as client:
        while True:
            try:
                if client.get(f"https://{host}/api/v2.0/ping").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if clock() >= deadline:
                raise PublishError("Harbor did not become ready in time")
            sleep(poll_seconds)


def copy_and_verify(source, digest, destination_repository, tag, src_authfile, dest_authfile, skopeo=run_skopeo):
    destination = f"docker://{destination_repository}:{tag}"
    skopeo(["copy", "--all", "--preserve-digests", "--retry-times", "3", "--src-authfile", src_authfile,
            "--dest-authfile", dest_authfile, f"docker://{source}@{digest}", destination])
    manifest = skopeo(["inspect", "--raw", "--authfile", dest_authfile, destination])
    actual = "sha256:" + hashlib.sha256(manifest).hexdigest()
    if actual != digest:
        raise PublishError("Harbor manifest digest does not match the verified ACR digest")
    return actual


def _env(name, pattern):
    value = os.environ.get(name, "")
    if not pattern.fullmatch(value):
        raise PublishError(f"{name} is missing or invalid")
    return value


def _read(path_variable):
    path = os.environ.get(path_variable, "")
    if not path or not os.path.isfile(path):
        raise PublishError(f"{path_variable} must point to a file")
    return path


def write_harbor_authfile(directory, host, username, password):
    path = os.path.join(directory, "harbor-auth.json")
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"auths": {host: {"auth": auth}}}, handle)
    return path


def main(skopeo=run_skopeo, transport=None, sleep=time.sleep):
    if os.environ.get("CI_DEBUG_TRACE") == "true":
        raise PublishError("Refusing to run with CI debug tracing enabled")
    repo = _env("GITHUB_REPOSITORY", _REPOSITORY)
    commit = _env("CI_COMMIT_SHA", _COMMIT)
    registry = _env("ACR_REGISTRY", re.compile(r"[a-z0-9-]+\.azurecr\.io"))
    harbor_repository = _env("HARBOR_IMAGE_REPOSITORY", _PATH)
    harbor_host = _env("HARBOR_HOST", _HOST)
    harbor_user = _env("HARBOR_USERNAME", re.compile(r"[A-Za-z0-9$+_.@-]{1,100}"))
    src_authfile = _read("ACR_AUTHFILE")
    with open(_read("HARBOR_PASSWORD_FILE"), encoding="utf-8") as handle:
        harbor_password = handle.read().strip()
    with open(_read("GITHUB_TOKEN_FILE"), encoding="utf-8") as handle:
        token = handle.read().strip()
    if not harbor_password:
        raise PublishError("HARBOR_PASSWORD_FILE is empty")
    timeout = int(os.environ.get("GITHUB_WAIT_SECONDS", "1200"))
    github = GitHub(token or None, transport=transport)
    candidate = wait_for_candidate(github, repo, commit, timeout, int(os.environ.get("GITHUB_POLL_SECONDS", "30")), sleep=sleep)
    evidence = fetch_evidence(github, repo, commit, candidate, registry)
    destination = f"{harbor_host}/{harbor_repository}"
    wait_for_harbor(harbor_host, int(os.environ.get("HARBOR_WAIT_SECONDS", "900")), 15, transport=transport, sleep=sleep)
    with tempfile.TemporaryDirectory() as directory:
        dest_authfile = write_harbor_authfile(directory, harbor_host, harbor_user, harbor_password)
        digest = copy_and_verify(evidence.repository, evidence.digest, destination, f"git-{commit[:12]}",
                                 src_authfile, dest_authfile, skopeo)
    provenance = {
        "schema_version": 1, "commit_sha": commit,
        "github": {"repository": repo, "run_id": candidate.run_id, "run_attempt": candidate.attempt},
        "image": {"source": evidence.repository, "destination": destination, "tag": f"git-{commit[:12]}", "digest": digest},
    }
    with open("image-provenance.json", "w", encoding="utf-8") as handle:
        json.dump(provenance, handle, indent=2)
    print(f"Published {destination}@{digest}")


if __name__ == "__main__":
    try:
        main()
    except (PublishError, httpx.HTTPError, ValueError) as error:
        print(f"Image publication failed: {error}", file=sys.stderr)
        sys.exit(1)
