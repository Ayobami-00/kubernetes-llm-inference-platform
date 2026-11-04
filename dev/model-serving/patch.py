"""Apply a bounded CA/tooling-only patch to an exact upstream vLLM image."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
PATCHED = {"certifi", "pip", "uv"}


def settings(path):
    value = json.loads(path.read_text())
    if (
        value.get("schema_version") != "klip-vllm-security-patch.v1"
        or not re.fullmatch(r"docker.io/vllm/vllm-openai@sha256:[a-f0-9]{64}", value["base"])
        or {w["name"] for w in value["wheels"]} != PATCHED
        or len(value["wheels"]) != len(PATCHED)
    ):
        raise ValueError("Unexpected serving patch scope")
    for wheel in value["wheels"]:
        if (
            not re.fullmatch(r"[a-zA-Z0-9_.-]+\.whl", wheel["filename"])
            or not wheel["filename"].startswith(wheel["name"] + "-" + wheel["version"] + "-")
            or not wheel["url"].startswith("https://files.pythonhosted.org/packages/")
            or not wheel["url"].endswith("/" + wheel["filename"])
            or not re.fullmatch(r"[a-f0-9]{64}", wheel["sha256"])
            or not 0 < wheel["size"] <= 32 * 1024 * 1024
        ):
            raise ValueError("Unpinned or oversized serving patch wheel")
    return value


def fetch(wheel, directory):
    target = directory / wheel["filename"]
    if target.exists():
        data = target.read_bytes()
    else:
        with urllib.request.urlopen(wheel["url"], timeout=90) as response:
            if not response.geturl().startswith("https://files.pythonhosted.org/packages/"):
                raise ValueError("Unexpected wheel download redirect")
            data = response.read(wheel["size"] + 1)
    if len(data) != wheel["size"] or hashlib.sha256(data).hexdigest() != wheel["sha256"]:
        raise ValueError("Serving patch wheel checksum/size differs")
    target.write_bytes(data)
    return target


def distributions():
    return {
        d.metadata["Name"].lower().replace("_", "-"): d.version
        for d in importlib.metadata.distributions()
    }


def assert_unchanged(before, after, protected):
    if {k: v for k, v in before.items() if k not in PATCHED} != {
        k: v for k, v in after.items() if k not in PATCHED
    }:
        raise ValueError("Security patch changed a model/runtime dependency")
    if any(after.get(k) != v for k, v in protected.items()):
        raise ValueError("Unexpected upstream model/runtime version")


def headers():
    """Retain native package evidence; this function does not waive findings."""
    version = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", "linux-libc-dev"], text=True
    ).strip()
    names = subprocess.check_output(["dpkg-query", "-L", "linux-libc-dev"], text=True)
    files = []
    for name in names.splitlines():
        path = Path(name)
        metadata = path.lstat()
        item = {"path": name, "mode": stat.S_IMODE(metadata.st_mode)}
        if path.is_symlink():
            item.update(kind="symlink", target=os.readlink(path))
        elif path.is_file():
            item.update(kind="file", sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        elif path.is_dir():
            item.update(kind="directory")
        else:
            raise ValueError("Unexpected kernel header package file type")
        files.append(item)
    if not files:
        raise ValueError("No native kernel-header inventory")
    return {"package": "linux-libc-dev", "version": version, "files": files}


def verify(value, receipt):
    after = distributions()
    assert_unchanged(receipt["before"], after, value["protected_versions"])
    if any(after.get(w["name"]) != w["version"] for w in value["wheels"]):
        raise ValueError("Serving patch installation differs")
    # The vendored CA bundle is independently affected; updating certifi alone is insufficient.
    import pip._vendor.certifi

    if pip._vendor.certifi.__version__ != value["pip_vendored_certifi"]:
        raise ValueError("Unexpected pip vendored CA bundle")
    import certifi

    if not Path(certifi.where()).is_file():
        raise ValueError("Patched CA bundle is absent")
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        if name in os.environ and os.environ[name] != certifi.where():
            raise ValueError("Runtime CA bundle differs from the patched bundle")


def install(value):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Install only during the Linux serving-image build")
    before = distributions()
    assert_unchanged(before, before, value["protected_versions"])
    with tempfile.TemporaryDirectory(prefix="klip-model-wheels-") as temporary:
        wheel_directory = Path(temporary)
        for wheel in value["wheels"]:
            fetch(wheel, wheel_directory)
        requirements = wheel_directory / "requirements.txt"
        requirements.write_text(
            "".join(
                f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}\n"
                for w in value["wheels"]
            )
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--break-system-packages",
                "--no-deps",
                "--no-index",
                "--disable-pip-version-check",
                "--require-hashes",
                "--find-links",
                str(wheel_directory),
                "--requirement",
                str(requirements),
            ],
            check=True,
            timeout=180,
        )
    receipt = {
        "schema_version": "klip-vllm-security-patch-receipt.v1",
        "built_at": datetime.now(UTC).isoformat(),
        "lock_sha256": hashlib.sha256((DIRECTORY / "patch.lock.json").read_bytes()).hexdigest(),
        "base": value["base"],
        "before": before,
        "after": distributions(),
        "limitations": ["A patch receipt is not a vulnerability waiver or serving approval."],
    }
    verify(value, receipt)
    for filename, document in (
        ("patch-receipt.json", receipt),
        ("header-inventory.json", headers()),
    ):
        output = DIRECTORY / filename
        output.write_text(json.dumps(document, indent=2) + "\n")
        output.chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "verify"])
    args = parser.parse_args()
    configuration = settings(DIRECTORY / "patch.lock.json")
    if args.action == "install":
        install(configuration)
    else:
        verify(configuration, json.loads((DIRECTORY / "patch-receipt.json").read_text()))
