"""Operator primitives: explicit identities, private artifacts and checked subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra/terraform"
CONFIG = json.loads((TERRAFORM / "project.json").read_text())
ROOTS = {
    "bootstrap": TERRAFORM / "state-bootstrap",
    "foundation": TERRAFORM / "environments/aws-foundation",
    "dns": TERRAFORM / "environments/inference-dns",
    "backups": TERRAFORM / "environments/cluster-backups",
    "probe": TERRAFORM / "environments/network-probe",
    "platform": TERRAFORM / "environments/platform-workers",
}
STATE_KEYS = {
    "bootstrap": "state-bootstrap/terraform.tfstate",
    "foundation": "aws-foundation/terraform.tfstate",
    "dns": "inference-dns/terraform.tfstate",
    "backups": "cluster-backups/terraform.tfstate",
    "probe": "network-probe/terraform.tfstate",
    "platform": "platform-workers/terraform.tfstate",
}
PROFILES = {
    "bootstrap": "kubernetes-llm-bootstrap",
    "foundation": "kubernetes-llm-foundation",
    "dns": "kubernetes-llm-dns",
    "backups": "kubernetes-llm-foundation",
    "probe": "kubernetes-llm-foundation",
    "platform": "kubernetes-llm-foundation",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def state_home() -> Path:
    return Path(os.environ.get("KLIP_STATE_HOME", Path.home() / ".local/state" / CONFIG["project"]))


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private(path: Path, value: dict | list | str) -> None:
    private_dir(path.parent)
    content = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def environment(profile: str) -> dict[str, str]:
    result = dict(os.environ)
    if any(key.startswith("AWS_ENDPOINT_URL") for key in result):
        raise ValueError("Custom AWS endpoints are not permitted by the live operator tooling")
    for key in list(result):
        if key.startswith(("TF_CLI_ARGS", "TF_VAR_")) or key == "TF_DATA_DIR":
            result.pop(key)
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_DEFAULT_PROFILE",
    ):
        result.pop(key, None)
    result.update(
        {
            "AWS_PROFILE": profile,
            "AWS_REGION": CONFIG["region"],
            "AWS_DEFAULT_REGION": CONFIG["region"],
            "AWS_PAGER": "",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CLI_AUTO_PROMPT": "off",
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "PATH": str(ROOT / "artifacts/tools/bin") + os.pathsep + result.get("PATH", ""),
            "LC_ALL": "en_US.UTF-8" if platform.system() == "Darwin" else "C.UTF-8",
            "ANSIBLE_HOME": str(ROOT / "artifacts/ansible"),
            "TF_PLUGIN_CACHE_DIR": str(ROOT / "artifacts/terraform-provider-cache"),
        }
    )
    return result


def run(arguments: list[str], *, profile: str | None = None, capture: bool = True) -> str:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment(profile) if profile else None,
        capture_output=capture,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            (result.stderr or result.stdout or f"Command failed: {arguments[0]}").strip()
        )
    return result.stdout or ""


class Aws:
    def __init__(self, profile: str, region: str | None = None):
        self.profile = profile
        self.region = region or CONFIG["region"]
        self.executable = shutil.which("aws") or "/opt/homebrew/bin/aws"

    def call(self, *arguments: str, missing: bool = False) -> dict | list | None:
        result = subprocess.run(
            [
                self.executable,
                *arguments,
                "--profile",
                self.profile,
                "--region",
                self.region,
                "--no-cli-pager",
                "--output",
                "json",
                "--cli-connect-timeout",
                "10",
                "--cli-read-timeout",
                "30",
            ],
            env=environment(self.profile),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            absent = ("(NoSuchEntity)", "(NoSuchKey)", "(NoSuchBucket)", "(404)", "(NotFound)")
            if missing and any(code in result.stderr for code in absent):
                return None
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def preflight(self, root: str) -> dict:
        identity = self.call("sts", "get-caller-identity")
        if identity["Account"] != CONFIG["account_id"]:
            raise ValueError("Wrong AWS account; refusing to proceed")
        if root == "bootstrap":
            expected = f"arn:aws:iam::{CONFIG['account_id']}:user/{CONFIG['source_user']}"
            valid = identity["Arn"] == expected
        else:
            name = (
                "kubernetes-llm-foundation-deployer"
                if root in {"foundation", "backups", "probe", "platform"}
                else "kubernetes-llm-inference-dns-deployer"
            )
            valid = identity["Arn"].startswith(
                f"arn:aws:sts::{CONFIG['account_id']}:assumed-role/{name}/"
            )
        if not valid:
            raise ValueError(f"Unexpected {root} identity: {identity['Arn']}")
        return identity


def tool(name: str) -> str:
    path = ROOT / "artifacts/tools/bin" / name
    if not path.is_file():
        raise ValueError("Install pinned tools with make infra-tools first")
    return str(path)


def tf(root: str, *arguments: str, profile: str | None = None) -> str:
    (ROOT / "artifacts/terraform-provider-cache").mkdir(parents=True, exist_ok=True)
    return run(
        [tool("terraform"), f"-chdir={ROOTS[root]}", *arguments], profile=profile or PROFILES[root]
    )


def source_digest() -> str:
    paths = sorted(
        path
        for path in TERRAFORM.rglob("*")
        if path.is_file()
        and ".terraform" not in path.parts
        and not path.name.endswith("_backend_override.tf")
        and (path.suffix in {".tf", ".json"} or path.name == ".terraform.lock.hcl")
    )
    paths += sorted((ROOT / "scripts/infrastructure").glob("*.py"))
    for folder in (ROOT / "infra/ansible", ROOT / "deploy"):
        paths += sorted(path for path in folder.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def ensure_ssh_key() -> tuple[str, Path]:
    folder = private_dir(state_home() / "credentials")
    key = folder / "foundation_ed25519"
    public = key.with_suffix(".pub")
    if not key.exists():
        if public.exists():
            raise ValueError("Public key exists without its private key; refusing to replace it")
        run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-a",
                "100",
                "-N",
                "",
                "-C",
                "klip-foundation",
                "-f",
                str(key),
            ]
        )
    derived = run(["ssh-keygen", "-y", "-f", str(key)]).strip()
    if not public.is_file() or public.read_text().split()[:2] != derived.split()[:2]:
        raise ValueError("The existing SSH key pair is incomplete or inconsistent")
    key.chmod(0o600)
    write_private(
        state_home() / "credentials.json",
        {
            CONFIG["ssh_credential_ref"]: {
                "private_key_path": str(key),
                "public_key_path": str(public),
            }
        },
    )
    return public.read_text().strip(), key
