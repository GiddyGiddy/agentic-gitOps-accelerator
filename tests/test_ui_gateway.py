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
