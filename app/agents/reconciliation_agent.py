import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class Deployment:
    application: str
    chart_version: str
    repository: str


def parse_deploy_csv(content: bytes) -> list[Deployment]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
    required = {"application", "chart_version", "repository"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("CSV requires application, chart_version and repository columns")
    deployments = []
    for row in reader:
        values = [row.get(key, "").strip() for key in ("application", "chart_version", "repository")]
        if not all(values):
            raise ValueError("CSV row contains a missing deployment field")
        deployments.append(Deployment(*values))
    if not deployments:
        raise ValueError("CSV has no deployments")
    return deployments


def application_is_healthy(application: dict, expected_version: str) -> bool:
    status = application.get("status", {})
    source = application.get("spec", {}).get("source", {})
    return (
        status.get("sync", {}).get("status") == "Synced"
        and status.get("health", {}).get("status") == "Healthy"
        and status.get("sync", {}).get("revision") == expected_version
        and source.get("targetRevision") == expected_version
    )
