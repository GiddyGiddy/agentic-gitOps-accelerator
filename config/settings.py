from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GITOPS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/gitops"
    gitlab_url: str = ""
    gitlab_token: str = Field(default="", repr=False)
    gitlab_token_file: str = ""
    gitlab_webhook_secret: str = ""
    gitlab_webhook_enabled: bool = True
    gitlab_poll_project: str = ""
    gitlab_poll_seconds: float = Field(default=60.0, gt=0)
    gitlab_build_poll_enabled: bool = False
    gitlab_build_poll_seconds: float = Field(default=60.0, ge=30)
    traceability_enabled: bool = False
    traceability_build_jobs: str = ""
    traceability_deploy_jobs: str = ""
    traceability_manifest_path: str = "gitops-provenance.json"
    traceability_harbor_project: str = ""
    traceability_harbor_repository: str = ""
    traceability_jira_prefixes: str = ""
    traceability_patch_directory: str = "chart-patch-files"
    argocd_url: str = ""
    argocd_token: str = ""
    argocd_webhook_secret: str = ""
    harbor_url: str = ""
    harbor_username: str = ""
    harbor_password: str = ""
    deploy_csv_path: str = "deploy_output.csv"
    teams_gateway_secret: str = ""
    teams_gateway_url: str = ""
    teams_approvers: str = ""
    status_secret: str = ""
    query_enabled: bool = False
    query_secret: str = Field(default="", repr=False)
    query_applications: str = ""
    query_projects: str = ""
    bundle_enabled: bool = False
    bundle_gateway_secret: str = Field(default="", repr=False)
    bundle_image_repositories: str = ""
    bundle_config_projects: str = ""
    rollback_enabled: bool = False
    rollback_gateway_secret: str = Field(default="", repr=False)
    rollback_targets: str = ""
    write_enabled: bool = False
    config_dir: str = "config"
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    mcp_mode: Literal["direct", "mcp"] = "direct"
    mcp_base_url: str = ""
    mcp_bearer_token: str = ""
    mcp_allow_insecure_loopback: bool = False
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*"
    mcp_allowed_origins: str = ""
    llm_enabled: bool = False
    llm_provider: Literal["azure_openai", "self_hosted"] = "azure_openai"
    llm_model: str = ""
    llm_azure_endpoint: str = ""
    llm_azure_auth: Literal["managed_identity", "workload_identity", "api_key"] = "managed_identity"
    llm_azure_client_id: str = ""
    llm_azure_api_key: str = Field(default="", repr=False)
    llm_self_hosted_base_url: str = ""
    llm_self_hosted_api_key: str = Field(default="", repr=False)
    llm_allow_insecure_loopback: bool = False
    llm_timeout_seconds: float = Field(default=20, gt=0, le=60)
    ui_enabled: bool = False
    ui_public_origin: str = ""
    ui_entra_tenant_id: str = ""
    ui_entra_client_id: str = ""
    ui_entra_client_secret: str = Field(default="", repr=False)
    ui_entra_client_secret_file: str = ""
    ui_allowed_users: str = ""

    @property
    def approvers(self) -> set[str]:
        return {value.strip() for value in self.teams_approvers.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.gitlab_token_file:
        settings.gitlab_token = _read_secret(settings.gitlab_token_file, "GitLab token")
    if settings.ui_entra_client_secret_file:
        settings.ui_entra_client_secret = _read_secret(
            settings.ui_entra_client_secret_file, "Entra client secret"
        )
    return settings


def _read_secret(path: str, name: str) -> str:
    value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    if not value or value != value.strip():
        raise RuntimeError(f"The {name} file is empty or contains surrounding whitespace")
    return value
