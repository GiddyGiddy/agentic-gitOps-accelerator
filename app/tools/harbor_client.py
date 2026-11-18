from urllib.parse import quote

import httpx


class HarborClient:
    def __init__(self, base_url: str, username: str, password: str):
        if not base_url.startswith("https://") or not username or not password:
            raise ValueError("Harbor HTTPS URL and credentials are required")
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v2.0/",
            auth=(username, password),
            timeout=30,
            follow_redirects=False,
        )

    def artifact(self, project: str, repository: str, reference: str) -> dict:
        response = self.client.get(
            f"projects/{quote(project, safe='')}/repositories/"
            f"{quote(repository, safe='')}/artifacts/{quote(reference, safe='')}"
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self.client.close()
