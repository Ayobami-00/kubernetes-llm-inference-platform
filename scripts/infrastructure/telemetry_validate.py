"""Validate rendered backend configuration with exact upstream binaries; no cloud access."""

from __future__ import annotations

import json
import subprocess

import yaml
from common import ROOT
from observability_release import BACKENDS, CACHE, image, render


def main():
    folder = ROOT / "artifacts/security/telemetry-config"
    folder.mkdir(parents=True, exist_ok=True)
    results = {}
    for component, key, flags in (
        ("loki", "config.yaml", ["-verify-config=true"]),
        ("tempo", "tempo.yaml", ["-config.verify", "-config.verify-errors-only"]),
        ("otel", "relay", ["validate"]),
    ):
        render(component)
        documents = [
            d for d in yaml.safe_load_all((CACHE / (component + "-rendered.yaml")).read_text()) if d
        ]
        candidates = [
            d["data"][key]
            for d in documents
            if d["kind"] == "ConfigMap" and key in d.get("data", {})
        ]
        if len(candidates) != 1:
            raise ValueError("Expected exactly one native component configuration")
        path = folder / (component + ".yaml")
        path.write_text(candidates[0])
        args = [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--env",
            "MY_POD_IP=127.0.0.1",
            "--env",
            "AWS_EC2_METADATA_DISABLED=true",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--tmpfs",
            "/var/lib/otelcol:rw,noexec,nosuid,size=32m,mode=1777",
            "--mount",
            f"type=bind,source={folder.resolve()},target=/inputs,readonly",
            image(BACKENDS[component]),
        ]
        args += (
            flags + ["--config=/inputs/otel.yaml"]
            if component == "otel"
            else ["-config.file=/inputs/" + component + ".yaml", *flags]
        )
        result = subprocess.run(args, text=True, capture_output=True, timeout=240, check=False)
        (folder / (component + ".log")).write_text(result.stdout + result.stderr)
        results[component] = {
            "image": image(BACKENDS[component]),
            "exit_code": result.returncode,
            "scope": (
                "Native config validation only; network disabled "
                "and no cloud/application credentials"
            ),
        }
        (folder / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError(
                "Native " + component + " validation failed; inspect its retained log"
            )
        print(component, "native validation passed", flush=True)


if __name__ == "__main__":
    main()
