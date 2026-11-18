import base64
import hmac
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from app.tools.argocd_client import ArgoCDClient
from app.tools.gitlab_client import GitLabClient
from app.tools.harbor_client import HarborClient
from config.settings import Settings, get_settings

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class BearerAuthentication:
    def __init__(self, app: ASGIApp, token: str):
        if not token:
            raise ValueError("GITOPS_MCP_BEARER_TOKEN must be configured")
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = [(key.lower(), value) for key, value in scope.get("headers", [])]
            values = [value for key, value in headers if key == b"authorization"]
            parts = values[0].split(b" ", 1) if len(values) == 1 else []
            if len(parts) != 2 or parts[0].lower() != b"bearer" or not hmac.compare_digest(parts[1], self.token):
                response = Response(
                    "Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings) -> Starlette:
    if not settings.mcp_bearer_token:
        raise ValueError("GITOPS_MCP_BEARER_TOKEN must be configured")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[item.strip() for item in settings.mcp_allowed_hosts.split(",") if item.strip()],
        allowed_origins=[item.strip() for item in settings.mcp_allowed_origins.split(",") if item.strip()],
    )
    if not security.allowed_hosts:
        raise ValueError("At least one MCP allowed host is required")
    options: dict[str, Any] = {
        "stateless_http": True, "json_response": True, "streamable_http_path": "/mcp",
        "transport_security": security,
    }
    gitlab = FastMCP("gitlab-read-only", **options)
    harbor = FastMCP("harbor-read-only", **options)
    argocd = FastMCP("argocd-read-only", **options)
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    )

    @gitlab.tool(structured_output=True, annotations=read_only)
    def gitlab_mr_changes(project_id: int, mr_iid: int) -> dict[str, Any]:
        """Read a merge request's changes from GitLab."""
        if project_id <= 0 or mr_iid <= 0:
            raise ValueError("Project and MR IDs must be positive")
        client = GitLabClient(settings.gitlab_url, settings.gitlab_token, False)
        try:
            return {"changes": client.changes(project_id, mr_iid)}
        finally:
            client.close()

    @gitlab.tool(structured_output=True, annotations=read_only)
    def gitlab_job_artifact(project_id: int, job_id: int, path: str) -> dict[str, Any]:
        """Read a GitLab job artifact file as base64."""
        if project_id <= 0 or job_id <= 0:
            raise ValueError("Project and job IDs must be positive")
        client = GitLabClient(settings.gitlab_url, settings.gitlab_token, False)
        try:
            content = client.artifact_file(project_id, job_id, path, max_bytes=MAX_ARTIFACT_BYTES)
        finally:
            client.close()
        return {"content_base64": base64.b64encode(content).decode("ascii")}

    @harbor.tool(structured_output=True, annotations=read_only)
    def harbor_artifact(project: str, repository: str, reference: str) -> dict[str, Any]:
        """Read Harbor artifact metadata."""
        if not all((project, repository, reference)):
            raise ValueError("Project, repository and reference are required")
        client = HarborClient(settings.harbor_url, settings.harbor_username, settings.harbor_password)
        try:
            return {"artifact": client.artifact(project, repository, reference)}
        finally:
            client.close()

    @argocd.tool(structured_output=True, annotations=read_only)
    def argocd_application(name: str) -> dict[str, Any]:
        """Read an Argo CD Application."""
        if not name:
            raise ValueError("Application name is required")
        client = ArgoCDClient(settings.argocd_url, settings.argocd_token)
        try:
            return {"application": client.application(name)}
        finally:
            client.close()

    servers = (gitlab, harbor, argocd)
    applications = [server.streamable_http_app() for server in servers]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/health/live", health),
            *[Mount(f"/{name}", app=BearerAuthentication(application, settings.mcp_bearer_token))
              for name, application in zip(("gitlab", "harbor", "argocd"), applications, strict=True)],
        ],
        lifespan=lifespan,
    )


def main() -> None:
    uvicorn.run(create_app(get_settings()), host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
