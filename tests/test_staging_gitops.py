import re
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "deploy" / "staging"
CHART = ROOT / "deploy" / "helm" / "helix-gitops-agents"


def _read_yaml(path):
    with path.open(encoding="utf-8") as file:
        return yaml.safe_load(file)


def test_staging_application_is_restricted_and_manual():
    with (STAGING / "argocd-helix.yaml").open(encoding="utf-8") as file:
        project, application = yaml.safe_load_all(file)

    project_spec = project["spec"]
    app_spec = application["spec"]
    source = app_spec["source"]
    destination = app_spec["destination"]

    assert application["metadata"]["namespace"] == project["metadata"]["namespace"] == "argocd"
    assert app_spec["project"] == project["metadata"]["name"]
    assert project_spec["sourceRepos"] == [source["repoURL"]]
    assert project_spec["destinations"] == [destination]
    assert destination == {"server": "https://kubernetes.default.svc", "namespace": "gitops-staging"}
    assert project_spec["clusterResourceWhitelist"] == []
    assert {(item["group"], item["kind"]) for item in project_spec["namespaceResourceWhitelist"]} == {
        ("", "ServiceAccount"), ("", "Service"), ("apps", "Deployment"),
        ("secrets-store.csi.x-k8s.io", "SecretProviderClass"),
    }
    assert source["targetRevision"] == "main"
    assert source["helm"]["releaseName"] == "helix"
    assert "automated" not in app_spec["syncPolicy"]
    assert {"Prune=false", "FailOnSharedResource=true"} <= set(app_spec["syncPolicy"]["syncOptions"])
    assert not application["metadata"].get("finalizers")


def test_staging_application_uses_existing_overlays_and_digest():
    with (STAGING / "argocd-helix.yaml").open(encoding="utf-8") as file:
        _, application = yaml.safe_load_all(file)
    source = application["spec"]["source"]
    chart = ROOT / source["path"]
    values = [(chart / name).resolve() for name in source["helm"]["valueFiles"]]

    assert values == [
        STAGING / "ui-values.yaml",
        STAGING / "gitlab-polling-values.yaml",
        STAGING / "gitops-values.yaml",
        STAGING / "harbor-reader-values.yaml",
    ]
    assert all(path.is_file() for path in values)
    assert _read_yaml(values[0])["uiAllowedUsersSecret"] == "gitops-console-allowlist"
    assert _read_yaml(values[1])["gitlabPoller"]["enabled"] is True
    gitops_values = _read_yaml(values[2])
    assert gitops_values["image"]["repository"] == "harbor.giddyitsolutions.com/charts/agentic-gitops-staging"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", gitops_values["image"]["digest"])
    assert gitops_values["imagePullSecrets"] == [{"name": "harbor-image-pull"}]
    assert UUID(gitops_values["uiKeyVault"]["clientId"]).int != 0


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_default_chart_does_not_mount_query_credentials():
    result = subprocess.run(["helm", "template", "helix", str(CHART)], capture_output=True, text=True, check=True)
    resources = [item for item in yaml.safe_load_all(result.stdout) if item]
    api = next(item for item in resources if item["kind"] == "Deployment" and item["metadata"]["name"] == "helix-agents")
    api_env = {item["name"] for item in api["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "GITOPS_QUERY_SECRET" not in api_env
    assert "GITOPS_GITLAB_TOKEN_FILE" not in api_env
    assert not any(item["kind"] == "SecretProviderClass" for item in resources)


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_image_pull_secret_is_optional_and_applies_to_all_workloads():
    command = [
        "helm", "template", "helix", str(CHART),
        "--set", "gitlabPoller.enabled=true",
        "--set", "mcp.enabled=true",
        "--set", "mcp.existingSecret=mcp-test",
        "--set", "mcp.allowedHosts=mcp.example",
    ]
    for extra, expected in (([], None), (["--set", "imagePullSecrets[0].name=harbor-pull"], [{"name": "harbor-pull"}])):
        result = subprocess.run(command + extra, capture_output=True, text=True, check=True)
        resources = [item for item in yaml.safe_load_all(result.stdout) if item]
        deployments = [item for item in resources if item["kind"] == "Deployment"]
        assert {item["metadata"]["name"] for item in deployments} == {
            "helix-agents", "helix-worker", "helix-gitlab-poller", "helix-mcp",
        }
        for item in deployments:
            assert item["spec"]["template"]["spec"].get("imagePullSecrets") == expected


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_staging_harbor_image_and_pull_secret_are_rendered_for_all_workloads():
    command = ["helm", "template", "helix", str(CHART)]
    for name in ("ui-values.yaml", "gitlab-polling-values.yaml", "gitops-values.yaml", "harbor-reader-values.yaml"):
        command.extend(["-f", str(STAGING / name)])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    resources = [item for item in yaml.safe_load_all(result.stdout) if item]
    deployments = [item for item in resources if item["kind"] == "Deployment"]
    values = _read_yaml(STAGING / "gitops-values.yaml")
    image = f'{values["image"]["repository"]}@{values["image"]["digest"]}'
    assert {item["metadata"]["name"] for item in deployments} == {
        "helix-agents", "helix-worker", "helix-gitlab-poller",
    }
    for item in deployments:
        spec = item["spec"]["template"]["spec"]
        assert spec["imagePullSecrets"] == [{"name": "harbor-image-pull"}]
        assert spec["containers"][0]["image"] == image


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_staging_query_uses_console_identity_and_dedicated_secret():
    command = ["helm", "template", "helix", str(CHART)]
    for name in ("ui-values.yaml", "gitlab-polling-values.yaml", "gitops-values.yaml"):
        command.extend(["-f", str(STAGING / name)])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    resources = [item for item in yaml.safe_load_all(result.stdout) if item]
    api = next(item for item in resources if item["kind"] == "Deployment" and item["metadata"]["name"] == "helix-agents")
    worker = next(item for item in resources if item["kind"] == "Deployment" and item["metadata"]["name"] == "helix-worker")
    providers = {
        item["metadata"]["name"]: item for item in resources if item["kind"] == "SecretProviderClass"
    }
    api_spec = api["spec"]["template"]["spec"]
    api_container = api_spec["containers"][0]
    api_env = {item["name"]: item for item in api_container["env"]}
    worker_env = {item["name"] for item in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    api_volumes = {item["name"]: item for item in api_spec["volumes"]}
    values = _read_yaml(STAGING / "gitops-values.yaml")

    assert api_env["GITOPS_QUERY_ENABLED"]["value"] == "true"
    assert api_env["GITOPS_QUERY_PROJECTS"]["value"] == "Gideon/agentic-gitOps-accelerator"
    assert api_env["GITOPS_QUERY_APPLICATIONS"]["value"] == "helix"
    assert api_env["GITOPS_QUERY_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "gitops-query-secret", "key": "GITOPS_QUERY_SECRET",
    }
    assert "GITOPS_QUERY_SECRET" not in worker_env
    assert api_env["GITOPS_GITLAB_TOKEN_FILE"]["value"] == "/mnt/gitlab-token/gitlab-token"
    assert api_volumes["gitlab-token"]["csi"]["volumeAttributes"]["secretProviderClass"] == "helix-api-gitlab-token"
    assert providers["helix-api-gitlab-token"]["spec"]["parameters"]["clientID"] == values["uiKeyVault"]["clientId"]
    assert providers["helix-gitlab-token"]["spec"]["parameters"]["clientID"] != values["uiKeyVault"]["clientId"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_api_gitlab_mount_rejects_missing_identity_prerequisites():
    result = subprocess.run(
        ["helm", "template", "helix", str(CHART), "--set", "apiGitlabKeyVault.enabled=true"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "apiGitlabKeyVault requires gitlabKeyVault.enabled and uiKeyVault.enabled" in result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_query_secret_rejects_conflicting_sources():
    result = subprocess.run(
        ["helm", "template", "helix", str(CHART), "--set", "querySecret=external",
         "--set", "env.GITOPS_QUERY_SECRET=conflict"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Set GITOPS_QUERY_SECRET in either env or querySecret, not both" in result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_harbor_reader_mount_is_worker_only():
    command = [
        "helm", "template", "helix", str(CHART),
        "--set", "azureWorkloadIdentity.enabled=true",
        "--set", "azureWorkloadIdentity.clientId=worker-client",
        "--set", "harborKeyVault.enabled=true",
        "--set", "harborKeyVault.vaultName=test-vault",
        "--set", "harborKeyVault.secretName=reader-password",
        "--set", "harborKeyVault.tenantId=test-tenant",
        "--set", "harborKeyVault.url=https://harbor.example",
        "--set", "harborKeyVault.username=robot-reader",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    resources = [item for item in yaml.safe_load_all(result.stdout) if item]
    deployments = {
        item["metadata"]["name"]: item for item in resources if item["kind"] == "Deployment"
    }
    worker = deployments["helix-worker"]["spec"]["template"]["spec"]
    api = deployments["helix-agents"]["spec"]["template"]["spec"]
    worker_env = {item["name"]: item for item in worker["containers"][0]["env"]}
    api_env = {item["name"] for item in api["containers"][0]["env"]}
    provider = next(
        item for item in resources
        if item["kind"] == "SecretProviderClass" and item["metadata"]["name"] == "helix-harbor-reader"
    )
    assert worker_env["GITOPS_HARBOR_PASSWORD_FILE"]["value"] == "/mnt/harbor-reader/password"
    assert worker_env["GITOPS_HARBOR_USERNAME"]["value"] == "robot-reader"
    assert "GITOPS_HARBOR_PASSWORD_FILE" not in api_env
    assert any(v["name"] == "harbor-reader" for v in worker["volumes"])
    assert not any(v["name"] == "harbor-reader" for v in api.get("volumes", []))
    assert provider["spec"]["parameters"]["clientID"] == "worker-client"
    assert "reader-password" in provider["spec"]["parameters"]["objects"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_staging_harbor_reader_uses_worker_identity():
    command = ["helm", "template", "helix", str(CHART)]
    for name in ("ui-values.yaml", "gitlab-polling-values.yaml", "gitops-values.yaml", "harbor-reader-values.yaml"):
        command.extend(["-f", str(STAGING / name)])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    resources = [item for item in yaml.safe_load_all(result.stdout) if item]
    deployments = {
        item["metadata"]["name"]: item for item in resources if item["kind"] == "Deployment"
    }
    worker = deployments["helix-worker"]["spec"]["template"]["spec"]
    worker_env = {item["name"]: item for item in worker["containers"][0]["env"]}
    provider = next(
        item for item in resources
        if item["kind"] == "SecretProviderClass" and item["metadata"]["name"] == "helix-harbor-reader"
    )
    assert worker_env["GITOPS_HARBOR_PASSWORD_FILE"]["value"] == "/mnt/harbor-reader/password"
    assert provider["spec"]["parameters"]["clientID"] == _read_yaml(STAGING / "gitlab-polling-values.yaml")["azureWorkloadIdentity"]["clientId"]
    for name in ("helix-agents", "helix-gitlab-poller"):
        spec = deployments[name]["spec"]["template"]["spec"]
        assert not any(volume["name"] == "harbor-reader" for volume in spec.get("volumes", []))


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_harbor_reader_rejects_missing_identity_or_conflicting_env():
    base = [
        "helm", "template", "helix", str(CHART),
        "--set", "harborKeyVault.enabled=true",
        "--set", "harborKeyVault.url=https://harbor.example",
        "--set", "harborKeyVault.username=robot-reader",
        "--set", "harborKeyVault.vaultName=test-vault",
        "--set", "harborKeyVault.secretName=reader-password",
        "--set", "harborKeyVault.tenantId=test-tenant",
    ]
    result = subprocess.run(base, capture_output=True, text=True)
    assert result.returncode != 0
    assert "harborKeyVault requires azureWorkloadIdentity.enabled" in result.stderr
    result = subprocess.run(
        base + ["--set", "azureWorkloadIdentity.enabled=true", "--set", "azureWorkloadIdentity.clientId=worker-client",
                "--set", "env.GITOPS_HARBOR_URL=https://other.example"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Do not set Harbor connection values in env" in result.stderr
