"""Patch CA/tooling and remove unreferenced extras from an exact upstream vLLM image."""

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
REMOVED = {
    "opentelemetry-exporter-otlp-common": "0.66b1",
    "opentelemetry-exporter-http-transport": "0.66b1",
}
# Upstream deliberately overrides Torch's NCCL pin for DeepEPv2. No other
# dependency-check exception is accepted in the derived image.
NCCL_ISSUE = (
    "torch 2.13.0+cu130 has requirement nvidia-nccl-cu13==2.29.7; "
    'platform_system == "Linux", but you have nvidia-nccl-cu13 2.30.7.'
)
UPSTREAM_OTEL_ISSUE = (
    "opentelemetry-exporter-otlp-common 0.66b1 has requirement "
    "opentelemetry-sdk~=1.45.1, but you have opentelemetry-sdk 1.40.0."
)


def settings(path):
    value = json.loads(path.read_text())
    if (
        value.get("schema_version") != "klip-vllm-security-patch.v1"
        or not re.fullmatch(r"docker.io/vllm/vllm-openai@sha256:[a-f0-9]{64}", value["base"])
        or {w["name"] for w in value["wheels"]} != PATCHED
        or len(value["wheels"]) != len(PATCHED)
        or value.get("removed_unreferenced_packages") != REMOVED
    ):
        raise ValueError("Unexpected serving patch scope")
    for wheel in value["wheels"]:
        if (
            not re.fullmatch(r"[a-zA-Z0-9_.-]+\.whl", wheel["filename"])
            or not wheel["filename"].startswith(
                wheel["name"].replace("-", "_") + "-" + wheel["version"] + "-"
            )
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
    if {k: v for k, v in before.items() if k not in PATCHED | set(REMOVED)} != {
        k: v for k, v in after.items() if k not in PATCHED
    }:
        raise ValueError("Security patch changed a model/runtime dependency")
    if any(after.get(k) != v for k, v in protected.items()):
        raise ValueError("Unexpected upstream model/runtime version")


def assert_unreferenced(requirements):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    for name, entries in requirements.items():
        if name in REMOVED:
            continue
        # Check even optional/inactive requirements conservatively: this is
        # removal of orphaned extras, not a change to another installed package.
        if any(canonicalize_name(Requirement(r).name) in REMOVED for r in entries):
            raise ValueError("An installed package still requires an orphan candidate: " + name)


def dependency_issues(expected):
    check = subprocess.run(
        [sys.executable, "-m", "pip", "--disable-pip-version-check", "check"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    issues = sorted(line.strip() for line in check.stdout.splitlines() if line.strip())
    if check.returncode != 1 or issues != sorted(expected) or check.stderr.strip():
        # This fixed offline build command emits public package diagnostics only.
        # Retain them so an unexpected dependency cannot become a blind allowlist change.
        raise ValueError(
            "Dependency check differs from the exact recorded upstream exceptions: "
            + json.dumps(
                {"exit_code": check.returncode, "issues": issues, "diagnostic": check.stderr[:2000]}
            )
        )
    return {"exit_code": check.returncode, "issues": issues}


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
    dependency_issues([NCCL_ISSUE])
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GRPC
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HTTP
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.sdk.trace import TracerProvider

    assert GRPC and HTTP and PrometheusMetricReader
    provider = TracerProvider()
    with provider.get_tracer("klip-build-check").start_as_current_span("offline-import-check"):
        pass
    provider.shutdown()


def install(value):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Install only during the Linux serving-image build")
    before = distributions()
    if any(before.get(k) != v for k, v in value["protected_versions"].items()):
        raise ValueError("Unexpected upstream model/runtime version")
    baseline = dependency_issues([NCCL_ISSUE, UPSTREAM_OTEL_ISSUE])
    if any(before.get(k) != v for k, v in REMOVED.items()):
        raise ValueError("Unexpected orphaned telemetry package version")
    requirements = {
        d.metadata["Name"].lower().replace("_", "-"): d.requires or []
        for d in importlib.metadata.distributions()
    }
    assert_unreferenced(requirements)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--disable-pip-version-check",
            "uninstall",
            "--yes",
            *sorted(REMOVED),
        ],
        check=True,
        timeout=60,
    )
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
        "dependency_check_before": baseline,
        "dependency_check_after": dependency_issues([NCCL_ISSUE]),
        "removed_unreferenced_packages": REMOVED,
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
