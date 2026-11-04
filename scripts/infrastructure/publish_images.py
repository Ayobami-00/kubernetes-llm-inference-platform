"""Private CI image receipts. No registry passwords or OIDC tokens enter artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

COMPONENTS = {
    "backend": ("management-backend", "apps/management/backend/Dockerfile"),
    "web": ("management-web", "apps/management/web/Dockerfile"),
    "runner": ("workload-runner", "workload-runner/Dockerfile"),
    "stub": ("workload-runner", "dev/inference-stub/Dockerfile"),
}
ADDITIONAL_COMPONENTS = {
    "host-executor": ("management-backend", "dev/host-executor/Dockerfile"),
}
ALL_COMPONENTS = COMPONENTS | ADDITIONAL_COMPONENTS
REGISTRY = "127811062476.dkr.ecr.eu-west-2.amazonaws.com"


def identity(component: str, sha: str) -> tuple[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("An exact source commit is required")
    return "klip/" + ALL_COMPONENTS[component][0], component + "-" + sha


def aws(*arguments: str, absent: bool = False) -> dict | None:
    result = subprocess.run(
        [
            "aws",
            "ecr",
            *arguments,
            "--region",
            "eu-west-2",
            "--output",
            "json",
            "--no-cli-pager",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "30",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if absent and "ImageNotFoundException" in result.stderr:
            return None
        if (
            arguments[0] == "describe-image-scan-findings"
            and "ScanNotFoundException" in result.stderr
        ):
            return {"imageScanStatus": {"status": "PENDING"}}
        raise RuntimeError("ECR metadata read failed; credential-bearing diagnostics withheld")
    return json.loads(result.stdout)


def resolve(component: str, sha: str) -> dict:
    repository, tag = identity(component, sha)
    response = aws(
        "describe-images",
        "--repository-name",
        repository,
        "--image-ids",
        "imageTag=" + tag,
        absent=True,
    )
    details = response["imageDetails"] if response else []
    if response is not None and len(details) != 1:
        raise ValueError("Expected one immutable image for the source tag")
    digest = details[0]["imageDigest"] if details else None
    if digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid registry image digest")
    return {
        "repository": repository,
        "tag": tag,
        "digest": digest,
        "image": REGISTRY + "/" + repository + ":" + tag,
    }


def record(component: str, sha: str, *, sleep=time.sleep) -> Path:
    resolved = resolve(component, sha)
    if not resolved["digest"]:
        raise ValueError("Published image does not exist")
    scan = None
    for _ in range(30):
        scan = aws(
            "describe-image-scan-findings",
            "--repository-name",
            resolved["repository"],
            "--image-id",
            "imageDigest=" + resolved["digest"],
        )
        status = scan.get("imageScanStatus", {}).get("status")
        if status == "COMPLETE":
            break
        if status not in {"IN_PROGRESS", "PENDING"}:
            raise ValueError("Registry image scan did not complete: " + str(status))
        sleep(10)
    else:
        raise TimeoutError("Registry scan exceeded five minutes")
    receipt = {
        "schema_version": "klip-private-image.v1",
        "component": component,
        "source_commit": sha,
        **resolved,
        "pinned_image": REGISTRY + "/" + resolved["repository"] + "@" + resolved["digest"],
        "dockerfile_sha256": hashlib.sha256(
            Path(ALL_COMPONENTS[component][1]).read_bytes()
        ).hexdigest(),
        "lockfile_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
        "workflow_run_id": os.environ["GITHUB_RUN_ID"],
        "workflow_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "scan": scan,
        "limitations": ["A completed scan is evidence, not a deployment security approval"],
    }
    output = Path("artifacts/releases") / (component + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["resolve", "record"])
    parser.add_argument("component", choices=ALL_COMPONENTS)
    args = parser.parse_args()
    sha = os.environ["GITHUB_SHA"]
    if args.action == "record":
        print(record(args.component, sha))
    else:
        resolved = resolve(args.component, sha)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("exists=" + str(resolved["digest"] is not None).lower() + "\n")
            stream.write("image=" + resolved["image"] + "\n")


if __name__ == "__main__":
    main()
