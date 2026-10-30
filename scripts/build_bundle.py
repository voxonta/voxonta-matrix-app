"""Build an independently installable source bundle with a pinned capture worker.

Run from a checkout; no sibling repository is needed by the bundle recipient.
The builder accepts a local capture checkout, while CI fetches the pinned ref.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

PRODUCT = "matrix"
ROOT = Path(__file__).resolve().parents[1]
CAPTURE_REF = (ROOT / "deploy" / "capture.ref").read_text().strip()


def archive(repo, ref, destination):
    data = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", ref])
    with tarfile.open(fileobj=io.BytesIO(data)) as source:
        # The archive is a pinned local Git tree, never an uploaded tarball.
        for member in source.getmembers():
            target = destination / member.name
            if (
                not target.resolve().is_relative_to(destination.resolve())
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("unsafe source archive")
        if hasattr(tarfile, "data_filter"):
            source.extractall(destination, filter="data")
        else:
            # Python 3.9/early 3.11: all paths/types/links were validated above.
            source.extractall(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-source", type=Path, required=True)
    parser.add_argument("--app-archive", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[0-9a-f]{40}", CAPTURE_REF):
        parser.error("deploy/capture.ref must pin a full commit SHA")
    if PRODUCT == "nextcloud" and (not args.app_archive or not args.app_archive.is_file()):
        parser.error("Nextcloud bundle requires --app-archive from build-release.sh")
    own_ref = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "source"
        source.mkdir()
        archive(ROOT, own_ref, source)
        if (source / "deploy" / "capture.ref").read_text().strip() != CAPTURE_REF:
            raise ValueError("commit deploy/capture.ref before building a distribution")
        package = Path(temp) / f"voxonta-{PRODUCT}"
        package.mkdir()
        for name in ("compose.yaml", ".env.example", "README.md", "capture.ref"):
            shutil.copy2(source / "deploy" / name, package / name)
        capture = package / "capture"
        capture.mkdir()
        archive(args.capture_source, CAPTURE_REF, capture)
        extra = ".[livekit]" if PRODUCT == "matrix" else "."
        (capture / "Dockerfile").write_text(
            "FROM python:3.12-slim\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "libopus0 libvpx9 libsrtp2-1 && rm -rf /var/lib/apt/lists/*\nWORKDIR /app\n"
            "COPY pyproject.toml README.md LICENSE ./\nCOPY src ./src\n"
            f"RUN pip install --no-cache-dir '{extra}' && useradd --system voxonta\n"
            'ENV PYTHONUNBUFFERED=1\nUSER voxonta\nCMD ["python", "/app/src/main.py"]\n'
        )
        if PRODUCT == "matrix":
            integration = package / "integration"
            integration.mkdir()
            for item in ("src", "synapse", "docs"):
                shutil.copytree(
                    source / item,
                    integration / item,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            for item in ("pyproject.toml", "README.md", "LICENSE", "Dockerfile"):
                shutil.copy2(source / item, integration / item)
            shutil.copy2(source / "synapse" / "voxonta_widget_adapter.py", package)
        else:
            with tarfile.open(args.app_archive) as app:
                info = app.extractfile("voxonta/appinfo/info.xml")
                if info is None or b"<id>voxonta</id>" not in info.read():
                    raise ValueError("not a Voxonta Nextcloud application archive")
            shutil.copy2(args.app_archive, package / "voxonta-app.tar.gz")
        manifest = {
            "product": f"voxonta-{PRODUCT}",
            "integration_ref": own_ref,
            "capture_ref": CAPTURE_REF,
            "requires_other_integration": False,
        }
        (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        target = args.output / f"voxonta-{PRODUCT}-{own_ref[:7]}.tar.gz"
        with tarfile.open(target, "w:gz") as out:
            out.add(
                package,
                arcname=package.name,
                filter=lambda m: None if m.name.endswith(".DS_Store") else m,
            )
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        target.with_suffix(target.suffix + ".sha256").write_text(f"{digest}  {target.name}\n")
        print(target)


if __name__ == "__main__":
    main()
