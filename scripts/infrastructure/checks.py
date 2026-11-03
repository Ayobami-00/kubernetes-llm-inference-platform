"""Credential-free infrastructure checks, including generated IAM drift."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from common import CONFIG, ROOT, ROOTS, TERRAFORM, tool
from configuration import validate_configuration


def check_command(arguments: list[str]) -> None:
    print("Checking:", " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-platforms", action="store_true")
    args = parser.parse_args()
    validate_configuration(CONFIG)
    pins = json.loads((TERRAFORM / "toolchain.json").read_text())
    if (ROOT / ".terraform-version").read_text().strip() != pins["terraform"]["version"]:
        raise ValueError("Terraform version pin drift")
    # Mock tests must remain independent of local AWS credentials or metadata.
    for key in list(os.environ):
        if key.startswith(("AWS_", "TF_CLI_ARGS", "TF_VAR_")) or key == "TF_DATA_DIR":
            os.environ.pop(key)
    cache = ROOT / "artifacts/terraform-provider-cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "AWS_PROFILE": "klip-offline-tests",
            "AWS_EC2_METADATA_DISABLED": "true",
            "TF_INPUT": "0",
            "TF_IN_AUTOMATION": "1",
            "TFLINT_PLUGIN_DIR": str(ROOT / "artifacts/tflint/plugins"),
            "TF_PLUGIN_CACHE_DIR": str(cache),
        }
    )
    check_command(
        [
            sys.executable,
            str(ROOT / "scripts/infrastructure/aws_access.py"),
            "--account-id",
            CONFIG["account_id"],
            "--output-dir",
            str(TERRAFORM / "state-bootstrap/policies"),
            "--check",
        ]
    )
    check_command([tool("terraform"), "fmt", "-check", "-recursive", "infra/terraform"])
    check_command([tool("tflint"), f"--config={ROOT / '.tflint.hcl'}", "--init"])
    modules = sorted((TERRAFORM / "modules").iterdir())
    with tempfile.TemporaryDirectory(prefix="klip-terraform-checks-") as temporary:
        check_terraform([*ROOTS.values(), *modules], temporary, args.lock_platforms)
    check_command([sys.executable, "-m", "pytest", "-q"])
    check_command([str(ROOT / ".venv/bin/ruff"), "check", "."])
    check_command([str(ROOT / ".venv/bin/ruff"), "format", "--check", "."])
    check_command([sys.executable, "scripts/export-contracts.py", "--check"])


def check_terraform(paths: list, temporary: str, lock_platforms: bool) -> None:
    for index, path in enumerate(paths):
        os.environ["TF_DATA_DIR"] = f"{temporary}/{index}"
        override = path / "local_backend_override.tf"
        if override.exists():
            # The generated override points outside this checkout. Backend-free
            # init is safe; mocked plan tests never write to that backend.
            print(f"Using backend-free checks with existing local override: {path.name}")
        check_command(
            [tool("terraform"), f"-chdir={path}", "init", "-backend=false", "-input=false"]
        )
        if lock_platforms and index == 0:
            check_command(
                [
                    tool("terraform"),
                    f"-chdir={path}",
                    "providers",
                    "lock",
                    "-platform=darwin_arm64",
                    "-platform=linux_amd64",
                ]
            )
            # All roots/modules pin the same sole AWS provider. One signed download
            # for both platforms avoids unpacking repeated 700+ MB binaries.
            for other in paths[1:]:
                shutil.copyfile(path / ".terraform.lock.hcl", other / ".terraform.lock.hcl")
        check_command([tool("terraform"), f"-chdir={path}", "validate"])
        check_command([tool("tflint"), f"--chdir={path}", f"--config={ROOT / '.tflint.hcl'}"])
        if (path / "tests").is_dir():
            check_command([tool("terraform"), f"-chdir={path}", "test", "-no-color"])


if __name__ == "__main__":
    main()
