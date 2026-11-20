from urllib.parse import quote

import httpx


class ArgoCDClient:
    def __init__(self, base_url: str, token: str, ca_cert_file: str = ""):
        if not base_url.startswith("https://") or not token:
            raise ValueError("Argo CD HTTPS URL and token are required")
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            follow_redirects=False,
            verify=ca_cert_file or True,
        )

    def application(self, name: str) -> dict:
        response = self.client.get(f"applications/{quote(name, safe='')}")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self.client.close()
