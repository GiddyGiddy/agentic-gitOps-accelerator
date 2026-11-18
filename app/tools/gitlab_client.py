import hashlib
import json
from urllib.parse import quote

import httpx

from app.supervisor.guardrails import require_write_enabled


class GitLabClient:
    def __init__(self, base_url: str, token: str, write_enabled: bool = False):
        if not base_url.startswith("https://") or not token:
            raise ValueError("GitLab HTTPS URL and token are required")
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v4/",
            headers={"PRIVATE-TOKEN": token},
            timeout=30,
            follow_redirects=False,
        )
        self.write_enabled = write_enabled

    @staticmethod
    def _project(project: int | str) -> str:
        return quote(str(project), safe="")

    def changes(self, project: int | str, iid: int) -> list[dict]:
        response = self.client.get(f"projects/{self._project(project)}/merge_requests/{iid}/changes")
        response.raise_for_status()
        payload = response.json()
        if payload.get("overflow") or any(
            change.get("too_large") or change.get("collapsed") for change in payload["changes"]
        ):
            raise ValueError("GitLab returned an incomplete diff; routing cannot be validated")
        return payload["changes"]

    def project_id(self, project: int | str) -> int:
        response = self.client.get(f"projects/{self._project(project)}")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] <= 0:
            raise ValueError("GitLab returned an invalid project ID")
        return data["id"]

    def _pages(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        seen_pages: set[bytes] = set()
        for page in range(1, 10001):
            response = self.client.get(path, params={**params, "per_page": 100, "page": page})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise ValueError("GitLab returned an invalid paginated response")
            if not payload:
                return items
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()
            if fingerprint in seen_pages:
                raise ValueError("GitLab returned a repeated page")
            seen_pages.add(fingerprint)
            items.extend(payload)
        raise ValueError("GitLab pagination exceeded the safety limit")

    def merge_requests(self, project: int | str) -> list[dict]:
        return self._pages(
            f"projects/{self._project(project)}/merge_requests",
            {"scope": "all", "state": "all", "order_by": "created_at", "sort": "asc"},
        )

    def release_tags(self, project: int | str) -> list[dict]:
        return self._pages(
            f"projects/{self._project(project)}/repository/tags",
            {"search": "^REL/", "order_by": "name", "sort": "asc"},
        )

    def compare(self, project: int | str, from_ref: str, to_ref: str) -> dict:
        path = f"projects/{self._project(project)}/repository/compare"
        with self.client.stream("GET", path, params={"from": from_ref, "to": to_ref, "straight": "true"}) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > 2_000_000:
                    raise ValueError("GitLab comparison exceeds size limit")
                content.extend(chunk)
        payload = json.loads(content)
        if not isinstance(payload, dict) or not isinstance(payload.get("diffs"), list):
            raise ValueError("GitLab returned an invalid comparison")
        return payload

    def repository_file(self, project: int | str, path: str, ref: str) -> bytes:
        if not path or path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
            raise ValueError("Invalid repository file path")
        url = f"projects/{self._project(project)}/repository/files/{quote(path, safe='')}/raw"
        with self.client.stream("GET", url, params={"ref": ref}) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > 65536:
                    raise ValueError("Repository file exceeds size limit")
                content.extend(chunk)
            return bytes(content)

    def comment(self, project: int | str, iid: int, text: str) -> None:
        require_write_enabled(self.write_enabled)
        response = self.client.post(
            f"projects/{self._project(project)}/merge_requests/{iid}/notes", json={"body": text}
        )
        response.raise_for_status()

    def create_branch(self, project: int | str, branch: str, ref: str) -> dict:
        require_write_enabled(self.write_enabled)
        response = self.client.post(
            f"projects/{self._project(project)}/repository/branches",
            json={"branch": branch, "ref": ref},
        )
        response.raise_for_status()
        return response.json()

    def create_merge_request(self, project: int | str, source: str, target: str, title: str) -> dict:
        require_write_enabled(self.write_enabled)
        response = self.client.post(
            f"projects/{self._project(project)}/merge_requests",
            json={"source_branch": source, "target_branch": target, "title": title},
        )
        response.raise_for_status()
        return response.json()

    def play_job(self, project: int | str, job_id: int) -> dict:
        require_write_enabled(self.write_enabled)
        response = self.client.post(f"projects/{self._project(project)}/jobs/{job_id}/play")
        response.raise_for_status()
        return response.json()

    def artifact_file(
        self, project: int | str, job_id: int, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Invalid artifact path")
        url = f"projects/{self._project(project)}/jobs/{job_id}/artifacts/{quote(path, safe='/')}"
        with self.client.stream("GET", url) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if max_bytes is not None and len(content) + len(chunk) > max_bytes:
                    raise ValueError("Artifact exceeds size limit")
                content.extend(chunk)
            return bytes(content)

    def close(self) -> None:
        self.client.close()
