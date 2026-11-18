import subprocess
from pathlib import Path


def render_chart(chart: Path, values: Path, helm: str = "helm") -> str:
    """Run only inside a separately isolated, network-disabled CI runner."""
    if not chart.is_dir() or not values.is_file():
        raise ValueError(f"Chart or values file does not exist: {chart}, {values}")
    completed = subprocess.run(
        [helm, "template", "preflight", str(chart), "--values", str(values)],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
    )
    return completed.stdout
