import asyncio
import base64
import binascii
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from config.settings import Settings


class MCPToolError(RuntimeError):
    pass


class ReadOnlyMCPClient:
    def __init__(self, settings: Settings):
        parsed = urlsplit(settings.mcp_base_url)
        is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        valid_scheme = parsed.scheme == "https" or (
            parsed.scheme == "http" and is_loopback and settings.mcp_allow_insecure_loopback
        )
        if (not valid_scheme or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("MCP URL must use HTTPS, or explicitly permitted loopback HTTP")
        if not settings.mcp_bearer_token:
            raise ValueError("GITOPS_MCP_BEARER_TOKEN must be configured")
        self.base_url = settings.mcp_base_url.rstrip("/")
        self.token = settings.mcp_bearer_token

    async def _call(self, server: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(
            headers=headers, timeout=30, follow_redirects=False, trust_env=False
        ) as http_client:
            async with streamable_http_client(f"{self.base_url}/{server}/mcp", http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
        if result.isError:
            raise MCPToolError(f"MCP tool {name} failed: {result.content}")
        if not isinstance(result.structuredContent, dict):
            raise MCPToolError(f"MCP tool {name} returned invalid structured content")
        return result.structuredContent

    def call(self, server: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._call(server, name, arguments))

    def changes(self, project_id: int, mr_iid: int) -> list[dict]:
        result = self.call("gitlab", "gitlab_mr_changes", {"project_id": project_id, "mr_iid": mr_iid})
        changes = result.get("changes")
        if not isinstance(changes, list) or not all(isinstance(item, dict) for item in changes):
            raise MCPToolError("MCP GitLab changes response is invalid")
        return changes

    def artifact_file(self, project_id: int, job_id: int, path: str) -> bytes:
        result = self.call("gitlab", "gitlab_job_artifact", {
            "project_id": project_id, "job_id": job_id, "path": path,
        })
        encoded = result.get("content_base64")
        if not isinstance(encoded, str):
            raise MCPToolError("MCP artifact response is invalid")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MCPToolError("MCP artifact is not valid base64") from exc

    def application(self, name: str) -> dict:
        result = self.call("argocd", "argocd_application", {"name": name})
        application = result.get("application")
        if not isinstance(application, dict):
            raise MCPToolError("MCP Argo CD application response is invalid")
        return application

    def harbor_artifact(self, project: str, repository: str, reference: str) -> dict:
        result = self.call("harbor", "harbor_artifact", {
            "project": project, "repository": repository, "reference": reference,
        })
        artifact = result.get("artifact")
        if not isinstance(artifact, dict):
            raise MCPToolError("MCP Harbor artifact response is invalid")
        return artifact
