import argparse
import subprocess
import sys
from pathlib import Path

import yaml

from app.tools.helm_sandbox import render_chart


def configmap_keys(rendered: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for document in yaml.safe_load_all(rendered):
        if isinstance(document, dict) and document.get("kind") == "ConfigMap":
            name = document.get("metadata", {}).get("name")
            if not name or name in result:
                raise ValueError(f"ConfigMap name missing or duplicated: {name}")
            data = document.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError(f"ConfigMap {name} has invalid data")
            result[name] = set(data)
    return result


def validate_tenants(chart: Path, baseline: Path, tenants: list[Path]) -> list[str]:
    reference = configmap_keys(render_chart(chart, baseline))
    errors = []
    for tenant in tenants:
        try:
            actual = configmap_keys(render_chart(chart, tenant))
            for name, keys in reference.items():
                missing = keys - actual.get(name, set())
                if missing:
                    errors.append(f"{tenant}: ConfigMap {name} missing keys: {', '.join(sorted(missing))}")
        except (ValueError, yaml.YAMLError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{tenant}: {exc}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Render tenant charts in an isolated CI runner")
    parser.add_argument("--chart", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--tenant", required=True, action="append", type=Path)
    args = parser.parse_args()
    try:
        errors = validate_tenants(args.chart, args.baseline, args.tenant)
    except (ValueError, yaml.YAMLError, OSError, subprocess.SubprocessError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print("All tenant charts rendered successfully.")


if __name__ == "__main__":
    main()
