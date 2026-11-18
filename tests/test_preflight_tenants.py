from pathlib import Path

from app.agents import preflight_agent


def test_missing_configmap_key_is_reported_for_tenant(monkeypatch):
    baseline = "kind: ConfigMap\nmetadata:\n  name: config\ndata:\n  a: '1'\n  b: '2'\n"
    tenant = "kind: ConfigMap\nmetadata:\n  name: config\ndata:\n  a: '1'\n"
    monkeypatch.setattr(
        preflight_agent, "render_chart",
        lambda chart, values: baseline if values.name == "baseline.yaml" else tenant,
    )
    errors = preflight_agent.validate_tenants(Path("."), Path("baseline.yaml"), [Path("vbl.yaml")])
    assert len(errors) == 1
    assert "vbl.yaml" in errors[0] and "missing keys: b" in errors[0]


def test_render_failure_blocks_preflight(monkeypatch):
    def render(chart, values):
        if values.name == "broken.yaml":
            raise OSError("helm unavailable")
        return "kind: ConfigMap\nmetadata:\n  name: config\ndata: {}\n"

    monkeypatch.setattr(preflight_agent, "render_chart", render)
    assert "helm unavailable" in preflight_agent.validate_tenants(
        Path("."), Path("baseline.yaml"), [Path("broken.yaml")]
    )[0]
