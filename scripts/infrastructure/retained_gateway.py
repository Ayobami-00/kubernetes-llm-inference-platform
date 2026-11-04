"""Private authenticated llm-d entry point for the retained A10 experiment."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from common import ROOT, state_home, tool, utc_now, write_private
from platform_management.integrations.job_runner import frozen_file
from retained_model import checked_node

CONFIG = ROOT / "deploy/environments/lab/experiments/a10"
CHART = ROOT / "artifacts/platform/inference/llm-d-router-gateway-v1.0.0.tgz"
CHART_SHA = "ced5cbcf3ba336ff57c14e66a55c77ace1bd08525e2ce29c45026cb1a1254cc7"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review():
    record = json.loads((ROOT / "deploy/retained-a10-gateway-risk-review.json").read_text())
    now = datetime.now(UTC)
    if (
        not datetime.fromisoformat(record["reviewed_at"])
        <= now
        < datetime.fromisoformat(record["expires_at"])
    ):
        raise ValueError("Private experiment review expired")
    if record["scope"] != "private-authenticated-writerassist-experiment":
        raise ValueError("Unexpected review scope")
    for name, expected in record["configuration"].items():
        if sha(ROOT / name) != expected:
            raise ValueError("Gateway configuration differs from its review")
    for component in record["components"].values():
        directory = ROOT / component["directory"]
        report = json.loads((directory / "scan.json").read_text())
        summary = json.loads((directory / "summary.json").read_text())
        findings = [v for r in report["Results"] for v in r.get("Vulnerabilities", [])]
        high = {
            (v["VulnerabilityID"], v["PkgName"], v["InstalledVersion"])
            for v in findings
            if v["Severity"] == "HIGH"
        }
        if (
            sha(directory / "scan.json") != component["report_sha256"]
            or report["ArtifactName"] != component["image"]
            or summary["image"] != component["image"]
            or report["Metadata"]["ImageConfig"]["architecture"] != "amd64"
            or high != {tuple(v) for v in component["high_findings"]}
            or any(v["Severity"] == "CRITICAL" for v in findings)
            or not timedelta(0)
            <= now - datetime.fromisoformat(summary["completed_at"])
            <= timedelta(hours=24)
        ):
            raise ValueError("Unreviewed gateway image or native findings")
    if sha(CHART) != CHART_SHA:
        raise ValueError("Router chart checksum differs")
    return record


def deploy(evidence):
    from cluster import FOLDER, Host, checked, kubeconfigs, load_hosts, tunnel
    from host_executor_release import secret

    checked_node(evidence)
    assessed = review()
    root = evidence / "gateway"
    if (root / "result.json").exists():
        raise ValueError("Observe the saved gateway attempt; automatic replay is disabled")
    credential = state_home() / "credentials/writer-assist-inference-key"
    if not credential.exists():
        write_private(credential, secrets.token_urlsafe(48) + "\n")
    key = credential.read_text().strip()
    if len(key) < 48:
        raise ValueError("Inference credential is incomplete")
    cp = Host(load_hosts()["cp-01"])
    record = {"status": "deploying", "started_at": utc_now(), "review": assessed}
    write_private(root / "result.json", record)
    try:
        record["secret_uids"] = {
            "gateway": secret(cp, "llm-d", "writer-inference-auth", {"writer-assist": key}),
            "runner": secret(cp, "klip", "klip-writer-inference", {"api-key": key}),
        }
        documents = list(yaml.safe_load_all((CONFIG / "gateway.yaml").read_text()))
        # Require authentication before making the listener/route available.
        documents.sort(key=lambda d: d["kind"] != "SecurityPolicy")
        rendered = yaml.safe_dump_all(documents)
        frozen_file(root / "gateway.yaml", rendered.encode())
        record["apply"] = cp.run(
            ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "apply", "-f", "-"],
            data=rendered,
        )
        kubeconfigs()
        with tunnel():
            output = checked(
                [
                    tool("helm"),
                    "upgrade",
                    "--install",
                    "writer-assist-a10",
                    str(CHART),
                    "-n",
                    "llm-d",
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "--values",
                    str(CONFIG / "router-values.yaml"),
                    "--wait",
                    "--timeout",
                    "5m",
                    "--history-max",
                    "5",
                ],
                timeout=330,
            )
            write_private(root / "helm.log", output)
        record.update(status="deployed-requires-route-probes", completed_at=utc_now())
    except Exception as error:
        record.update(status="requires-observation", error_type=type(error).__name__)
        raise
    finally:
        write_private(root / "result.json", record)
    return {k: record[k] for k in ("status", "started_at", "secret_uids")}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    print(json.dumps(deploy(parser.parse_args().evidence.resolve()), indent=2))
