from pathlib import Path

import yaml


def test_staging_gateway_routes_only_console_over_https():
    path = Path(__file__).resolve().parents[1] / "deploy" / "staging" / "ui-gateway.yaml"
    resources = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    gateway = next(item for item in resources if item["kind"] == "Gateway")
    routes = [item for item in resources if item["kind"] == "HTTPRoute"]
    issuer = next(item for item in resources if item["kind"] == "Issuer")
    certificate = next(item for item in resources if item["kind"] == "Certificate")
    listeners = {item["name"]: item for item in gateway["spec"]["listeners"]}

    https = next(item for item in routes if item["spec"]["parentRefs"][0]["sectionName"] == "https")
    rules = https["spec"]["rules"]
    assert len(rules) == 1
    assert rules[0]["matches"] == [{"path": {"type": "PathPrefix", "value": "/console"}}]
    assert rules[0]["backendRefs"] == [{"name": "helix-agents", "port": 80}]
    assert listeners["https"]["tls"]["certificateRefs"][0]["name"] == certificate["spec"]["secretName"]
    assert https["spec"]["hostnames"] == certificate["spec"]["dnsNames"]

    http = next(item for item in routes if item["spec"]["parentRefs"][0]["sectionName"] == "http")
    assert all("backendRefs" not in rule for rule in http["spec"]["rules"])
    assert http["spec"]["rules"][0]["filters"][0]["requestRedirect"]["scheme"] == "https"
    challenge = issuer["spec"]["acme"]["solvers"][0]["http01"]["gatewayHTTPRoute"]
    assert challenge["parentRefs"][0]["sectionName"] == "http"


def test_staging_argocd_browser_preparation_is_restricted():
    staging = Path(__file__).resolve().parents[1] / "deploy" / "staging"
    access = list(yaml.safe_load_all((staging / "argocd-access.yaml").read_text(encoding="utf-8")))
    settings = next(item for item in access if item["metadata"]["name"] == "argocd-cm")["data"]
    rbac = next(item for item in access if item["metadata"]["name"] == "argocd-rbac-cm")["data"]
    oidc = yaml.safe_load(settings["oidc.config"])

    assert settings["admin.enabled"] == "false"
    assert settings["users.anonymous.enabled"] == "false"
    assert settings["accounts.gitops-agent"] == "apiKey"
    assert oidc["azure"]["useWorkloadIdentity"] is True
    assert rbac["policy.default"] == "role:agent-default"
    assert yaml.safe_load(rbac["scopes"]) == ["roles"]
    policies = [line.strip() for line in rbac["policy.csv"].splitlines()]
    assert "g, gitops-agent, role:gitops-agent-readonly" in policies
    assert "p, role:gitops-agent-readonly, applications, get, helix-staging/helix, allow" in policies
    assert "g, argocd.staging.reader, role:argocd-staging-reader" in policies
    reader_permissions = [line for line in policies if line.startswith("p, role:argocd-staging-reader,")]
    assert reader_permissions == [
        "p, role:argocd-staging-reader, applications, get, helix-staging/helix, allow",
        "p, role:argocd-staging-reader, projects, get, helix-staging, allow",
    ]

    policy = yaml.safe_load((staging / "argocd-backend-tls.yaml").read_text(encoding="utf-8"))
    validation = policy["spec"]["validation"]
    assert validation["subjectAltNames"] == [{"type": "Hostname", "hostname": validation["hostname"]}]
    assert validation["caCertificateRefs"] == [
        {"group": "", "kind": "ConfigMap", "name": "argocd-gateway-ca"}
    ]
    identity_patch = yaml.safe_load(
        (staging / "argocd-server-workload-identity-patch.yaml").read_text(encoding="utf-8")
    )
    assert identity_patch["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"] == "true"
