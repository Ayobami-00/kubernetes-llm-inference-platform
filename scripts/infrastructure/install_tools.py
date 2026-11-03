"""Install checksum-pinned tools into ignored, project-local storage."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def install() -> Path:
    pins = json.loads((ROOT / "infra/terraform/toolchain.json").read_text())
    machine = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    target = f"{platform.system().lower()}_{machine}"
    destination = ROOT / "artifacts/tools/bin"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("terraform", "tflint"):
        version = pins[name]["version"]
        expected = pins[name]["sha256"][target]
        marker = destination / f".{name}-{version}-{target}-{expected}"
        if marker.exists() and (destination / name).is_file():
            continue
        if name == "terraform":
            url = f"https://releases.hashicorp.com/terraform/{version}/terraform_{version}_{target}.zip"
        else:
            url = f"https://github.com/terraform-linters/tflint/releases/download/v{version}/tflint_{target}.zip"
        data = urllib.request.urlopen(url, timeout=120).read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Checksum mismatch for {name}; nothing installed")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read(name)
        temporary = destination / f".{name}.new"
        temporary.write_bytes(binary)
        temporary.chmod(0o755)
        os.replace(temporary, destination / name)
        marker.touch()
        print(f"Installed {name} {version}; SHA-256 verified")
    return destination


if __name__ == "__main__":
    print(install())
