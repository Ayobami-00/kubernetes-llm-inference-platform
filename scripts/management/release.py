"""Validate release versions and package charts with exact production image digests."""

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def validate(tag):
    match = re.fullmatch(r"management-v(\d+\.\d+\.\d+)", tag)
    if not match:
        raise ValueError("Use an explicit management-vX.Y.Z release tag")
    version = match[1]
    for name in ("klip", "klip-postgresql"):
        chart = yaml.safe_load((ROOT / "charts" / name / "Chart.yaml").read_text())
        if chart["version"] != version:
            raise ValueError("Chart version must match the management release tag")
    import tomllib

    backend = tomllib.loads((ROOT / "apps/management/backend/pyproject.toml").read_text())
    web = json.loads((ROOT / "apps/management/web/package.json").read_text())
    if backend["project"]["version"] != version or web["version"] != version:
        raise ValueError("Application versions must match the release tag")
    return version


def package(tag, repository, backend, web, commit, destination):
    version = validate(tag)
    if not all(
        isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value)
        for value in (backend, web)
    ):
        raise ValueError("Published image index digests are required")
    repository = repository.lower()
    images = {
        "backend": f"ghcr.io/{repository}/management-backend@{backend}",
        "web": f"ghcr.io/{repository}/management-web@{web}",
    }
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        for name in ("klip", "klip-postgresql"):
            chart = Path(temporary) / name
            shutil.copytree(ROOT / "charts" / name, chart)
            if name == "klip":
                values_path = chart / "values.yaml"
                values = yaml.safe_load(values_path.read_text())
                for key in ("backend", "web"):
                    values[key]["image"] = images[key]
                values_path.write_text(yaml.safe_dump(values, sort_keys=False))
            subprocess.run(
                ["helm", "package", str(chart), "--destination", str(destination)], check=True
            )
    manifest = {
        "version": version,
        "commit": commit,
        "images": images,
        "charts": [f"{name}-{version}.tgz" for name in ("klip", "klip-postgresql")],
    }
    (destination / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository")
    parser.add_argument("--backend-digest")
    parser.add_argument("--web-digest")
    parser.add_argument("--commit", default="")
    parser.add_argument("--destination", default="artifacts/releases")
    args = parser.parse_args()
    if args.repository:
        package(
            args.tag,
            args.repository,
            args.backend_digest,
            args.web_digest,
            args.commit,
            args.destination,
        )
    else:
        print(validate(args.tag))
