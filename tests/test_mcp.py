import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.mcp.client import MCPToolError, ReadOnlyMCPClient
from app.mcp.server import create_app
from app.storage.db import create_database
from app.storage.inbox import enqueue
from app.storage.migrate import migrate
from app.storage.models import InboxEvent
from app.supervisor.graph import build_graph
from app.tools.gitlab_client import GitLabClient
from app.worker import process_one
from config.settings import Settings


@pytest.fixture
def mcp_server(monkeypatch):
    class GitLab:
        def __init__(self, *args):
            pass

        def changes(self, project_id, mr_iid):
            return [{"new_path": "deployment/qa2/values.yaml", "diff": "+replicas: 2\n"}]

        def artifact_file(self, project_id, job_id, path, *, max_bytes):
            assert max_bytes > 0
            return b"application,chart_version,repository\nvn-base,260901.4,helix-argocd\n"

        def close(self):
            pass

    class Argo:
        def __init__(self, *args):
            pass

        def application(self, name):
            if name == "missing":
                raise ValueError("Application does not exist")
            return {"spec": {"source": {"targetRevision": "260901.4"}},
                    "status": {"sync": {"status": "Synced", "revision": "260901.4"},
                               "health": {"status": "Healthy"}}}

        def close(self):
            pass

    class Harbor:
        def __init__(self, *args):
            pass

        def artifact(self, project, repository, reference):
            return {"digest": "sha256:example"}

        def close(self):
            pass

    monkeypatch.setattr("app.mcp.server.GitLabClient", GitLab)
    monkeypatch.setattr("app.mcp.server.ArgoCDClient", Argo)
    monkeypatch.setattr("app.mcp.server.HarborClient", Harbor)
    settings = Settings(mcp_bearer_token="test-token")
    app = create_app(settings)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_authentication_discovery_and_tool_calls(mcp_server):
    async def discover(server):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer test-token"}) as http:
            async with streamable_http_client(f"{mcp_server}/{server}/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert all(tool.annotations is not None and tool.annotations.readOnlyHint for tool in tools)
                    return {tool.name for tool in tools}

    assert httpx.post(f"{mcp_server}/gitlab/mcp", json={}).status_code == 401
    assert httpx.post(
        f"{mcp_server}/gitlab/mcp", headers={"Authorization": "Bearer wrong"}, json={}
    ).status_code == 401
    assert httpx.post(
        f"{mcp_server}/gitlab/mcp",
        headers={"Authorization": "Bearer test-token", "Origin": "https://evil.example"}, json={},
    ).status_code == 403
    assert httpx.post(
        f"{mcp_server}/gitlab/mcp",
        headers={"Authorization": "Bearer test-token", "Host": "evil.example"}, json={},
    ).status_code == 421
    assert asyncio.run(discover("gitlab")) == {"gitlab_mr_changes", "gitlab_job_artifact"}
    assert asyncio.run(discover("harbor")) == {"harbor_artifact"}
    assert asyncio.run(discover("argocd")) == {"argocd_application"}

    client = ReadOnlyMCPClient(Settings(mcp_base_url=mcp_server, mcp_bearer_token="test-token",
                                        mcp_allow_insecure_loopback=True))
    assert client.changes(1, 2)[0]["new_path"] == "deployment/qa2/values.yaml"
    assert b"vn-base" in client.artifact_file(1, 2, "deploy.csv")
    assert client.application("vn-base")["status"]["health"]["status"] == "Healthy"
    assert client.harbor_artifact("project", "chart", "1.0")["digest"] == "sha256:example"
    with pytest.raises(MCPToolError):
        client.application("missing")


def test_authentication_and_transport_fail_closed():
    with pytest.raises(ValueError):
        create_app(Settings(mcp_bearer_token=""))
    with pytest.raises(ValueError):
        ReadOnlyMCPClient(Settings(mcp_base_url="http://example.org", mcp_bearer_token="x",
                                   mcp_allow_insecure_loopback=True))
    with pytest.raises(ValueError):
        ReadOnlyMCPClient(Settings(mcp_base_url="https://example.org", mcp_bearer_token=""))


def test_worker_reconciles_using_mcp(mcp_server, tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    engine, factory = create_database(url)
    settings = Settings(database_url=url, mcp_mode="mcp", mcp_base_url=mcp_server,
                        mcp_bearer_token="test-token", mcp_allow_insecure_loopback=True)
    event, _ = enqueue(factory, "gitlab", "job-mcp", {
        "kind": "job",
        "payload": {"project_id": 12, "build_id": 42, "build_name": "deploy_release_emea_vn_base",
                    "build_status": "success"},
    })
    assert process_one(factory, build_graph(InMemorySaver()), settings)
    with factory() as session:
        result = session.get(InboxEvent, event.id)
        assert result.status == "completed"
        assert result.result["deployments"][0]["healthy"] is True
    mr, _ = enqueue(factory, "gitlab", "mr-mcp", {
        "kind": "merge_request",
        "payload": {"project": {"id": 12}, "object_attributes": {"iid": 4, "target_branch": "master"}},
    })
    assert process_one(factory, build_graph(InMemorySaver()), settings)
    with factory() as session:
        result = session.get(InboxEvent, mr.id)
        assert result.status == "completed"
        assert "release environment" in result.result["violations"][0]
    settings.mcp_bearer_token = "invalid"
    failed, _ = enqueue(factory, "gitlab", "mr-invalid-token", {
        "kind": "merge_request",
        "payload": {"project": {"id": 12}, "object_attributes": {"iid": 5, "target_branch": "master"}},
    })
    assert process_one(factory, build_graph(InMemorySaver()), settings)
    with factory() as session:
        assert session.get(InboxEvent, failed.id).status == "failed"
    engine.dispose()


def test_artifact_stream_is_bounded():
    client = GitLabClient("https://gitlab.example", "token")
    client.client.close()
    client.client = httpx.Client(
        base_url="https://gitlab.example/api/v4/",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"12345")),
    )
    try:
        assert client.artifact_file(1, 2, "file.csv", max_bytes=5) == b"12345"
        with pytest.raises(ValueError, match="size limit"):
            client.artifact_file(1, 2, "file.csv", max_bytes=4)
        with pytest.raises(ValueError, match="Invalid artifact"):
            client.artifact_file(1, 2, "../file.csv", max_bytes=5)
    finally:
        client.close()
