"""Validate an extracted installation package without starting its services."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    root = Path(sys.argv[1]).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    matrix = manifest["product"] == "voxonta-matrix"
    values = {}
    for line in (root / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value or "local-package-check-not-a-production-secret"
    with tempfile.NamedTemporaryFile(mode="w") as env:
        env.write("\n".join(f"{k}={v}" for k, v in values.items()))
        env.flush()
        output = subprocess.check_output(
            [
                "docker",
                "compose",
                "--env-file",
                env.name,
                "-f",
                str(root / "compose.yaml"),
                "config",
                "--format",
                "json",
            ],
            env={**os.environ, **values},
        )
    services = json.loads(output)["services"]
    if matrix:
        assert set(services) == {"integration", "capture", "database"}
        assert not any(
            "NEXTCLOUD" in key
            for service in services.values()
            for key in service.get("environment", {})
        )
        assert (root / "voxonta_widget_adapter.py").is_file()
        assert (root / "integration/src/voxonta_matrix/web/index.html").is_file()
        assert (root / "integration/docs/widget.md").is_file()
        assert services["integration"]["environment"]["BOT_MODE_ENABLED"] == "false"
    else:
        assert set(services) == {"capture"}
        assert (root / "voxonta-app.tar.gz").is_file()
        assert not any(
            key.startswith(("MATRIX_", "LIVEKIT_", "WIDGET_"))
            for key in services["capture"]["environment"]
        )
        assert ".[livekit]" not in (root / "capture/Dockerfile").read_text()
    assert not manifest["requires_other_integration"]
    assert not (root / ".env").exists(), "real .env must never be distributed"
    assert (root / "capture/src/main.py").is_file()
    assert manifest["capture_ref"] == (root / "capture.ref").read_text().strip()
    print(manifest["product"] + ": independent installation configuration OK")


if __name__ == "__main__":
    main()
