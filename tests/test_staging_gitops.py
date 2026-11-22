import re
from pathlib import Path
from uuid import UUID

import yaml


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "deploy" / "staging"


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
    ]
    assert all(path.is_file() for path in values)
    assert _read_yaml(values[0])["uiAllowedUsersSecret"] == "gitops-console-allowlist"
    assert _read_yaml(values[1])["gitlabPoller"]["enabled"] is True
    gitops_values = _read_yaml(values[2])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", gitops_values["image"]["digest"])
    assert UUID(gitops_values["uiKeyVault"]["clientId"]).int != 0
