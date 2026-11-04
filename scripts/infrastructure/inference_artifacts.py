"""Fetch checksum-locked llm-d/gateway artifacts; never install or approve them."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from common import ROOT, tool

LOCK = ROOT / "deploy/inference.lock.json"
CACHE = ROOT / "artifacts/platform/inference"


def settings():
    value = json.loads(LOCK.read_text())
    if value.get("schema_version") != "klip-inference-artifacts.v1":
        raise ValueError("Unknown inference artifact lock")
    components = value["scan_components"]
    if set(components) != {
        "gateway",
        "proxy",
        "ai-controller",
        "ai-extproc",
        "llmd-epp",
        "vllm",
    } or set(components.values()) != set(value["images"]):
        raise ValueError("Inference scan coverage differs from the six locked components")
    for reference, image in value["images"].items():
        repository = reference.rsplit(":", 1)[0]
        if (
            image.get("reference") != reference
            or image.get("platform") != "linux/amd64"
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image.get("digest", ""))
            or image.get("image") != repository + "@" + image["digest"]
            or reference.endswith((":latest", ":main"))
            or not reference.startswith(("docker.io/", "ghcr.io/"))
        ):
            raise ValueError("Inference image identity/platform is unpinned")
    for chart in value["charts"].values():
        if (
            not re.fullmatch(r"[a-f0-9]{64}", chart["sha256"])
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", chart["version"])
            or not re.fullmatch(r"[a-z0-9.-]+\.tgz", chart["filename"])
            or not chart["repository"].startswith(("oci://docker.io/", "oci://ghcr.io/"))
        ):
            raise ValueError("Inference chart identity is unpinned")
    for artifact in value["artifacts"].values():
        if (
            not artifact["url"].startswith("https://")
            or "latest" in artifact["url"]
            or not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])
        ):
            raise ValueError("Inference source artifact is unpinned")
    return value


def checked_bytes(path, expected):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Inference artifact checksum differs: " + path.name)
    return data


def fetch():
    value = settings()
    CACHE.mkdir(parents=True, exist_ok=True)
    anonymous = CACHE / "anonymous-registry"
    anonymous.mkdir(mode=0o700, exist_ok=True)
    config = anonymous / "config.json"
    config.write_text("{}\n")
    config.chmod(0o600)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "DOCKER_", "HELM_")) and k not in {"GH_TOKEN", "GITHUB_TOKEN"}
    }
    env.update(DOCKER_CONFIG=str(anonymous.resolve()), HELM_REGISTRY_CONFIG=str(config.resolve()))
    for chart in value["charts"].values():
        path = CACHE / chart["filename"]
        if not path.exists():
            with tempfile.TemporaryDirectory(dir=CACHE) as temporary:
                subprocess.run(
                    [
                        tool("helm"),
                        "pull",
                        chart["repository"],
                        "--version",
                        chart["version"],
                        "--destination",
                        temporary,
                    ],
                    check=True,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                source = Path(temporary) / chart["filename"]
                if source.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("Inference chart exceeded its download bound")
                path.write_bytes(checked_bytes(source, chart["sha256"]))
        checked_bytes(path, chart["sha256"])
    for name, artifact in value["artifacts"].items():
        path = CACHE / (name + ".yaml")
        if not path.exists():
            with urllib.request.urlopen(artifact["url"], timeout=60) as response:
                data = response.read(3 * 1024 * 1024 + 1)
            if (
                len(data) > 3 * 1024 * 1024
                or hashlib.sha256(data).hexdigest() != artifact["sha256"]
            ):
                raise ValueError("Inference source artifact differs or exceeds its bound: " + name)
            path.write_bytes(data)
        checked_bytes(path, artifact["sha256"])
    return {
        "status": "fetched-and-verified",
        "versions": value["versions"],
        "charts": len(value["charts"]),
        "artifacts": len(value["artifacts"]),
        "limitations": value["limitations"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["fetch", "check"])
    args = parser.parse_args()
    print(json.dumps(fetch() if args.action == "fetch" else settings(), indent=2))
