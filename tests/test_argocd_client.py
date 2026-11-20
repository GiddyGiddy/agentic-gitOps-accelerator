from app.tools.argocd_client import ArgoCDClient
from config.settings import Settings


def test_argocd_client_uses_configured_ca_file(monkeypatch, tmp_path):
    ca_file = tmp_path / "argocd-ca.crt"
    ca_file.write_text("test CA", encoding="utf-8")
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.tools.argocd_client.httpx.Client", fake_client)

    ArgoCDClient("https://argocd.example", "read-only-token", str(ca_file))

    assert captured["verify"] == str(ca_file)
    assert captured["headers"] == {"Authorization": "Bearer read-only-token"}


def test_argocd_client_keeps_default_tls_verification(monkeypatch):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.tools.argocd_client.httpx.Client", fake_client)

    ArgoCDClient("https://argocd.example", "read-only-token")

    assert captured["verify"] is True


def test_argocd_token_is_hidden_from_settings_repr():
    assert "read-only-token" not in repr(Settings(argocd_token="read-only-token"))
