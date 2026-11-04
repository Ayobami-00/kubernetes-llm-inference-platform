"""Saved-plan update adding only the Cilium API-server entity policy to the executor release."""

import argparse
import fcntl
import json

import yaml
from common import ROOT, source_digest, state_home, tool, utc_now, write_private
from host_executor_release import folder, rendered, sha


def documents(text):
    values = [d for d in yaml.safe_load_all(text) if d]
    mapped = {(d["kind"], d["metadata"]["name"]): d for d in values}
    if len(mapped) != len(values):
        raise ValueError("Duplicate resource identities in a network update")
    return mapped


def validate_delta(previous, candidate):
    old, new = documents(previous), documents(candidate)
    if set(new) - set(old) != {("CiliumNetworkPolicy", "host-kube-api")} or any(
        new.get(k) != v for k, v in old.items()
    ):
        raise ValueError("Network update may only add the fixed API-server policy")
    spec = new[("CiliumNetworkPolicy", "host-kube-api")]["spec"]
    if spec != {
        "endpointSelector": {
            "matchLabels": {
                "app.kubernetes.io/name": "klip-host-executor",
                "app.kubernetes.io/component": "api",
            }
        },
        "egress": [
            {
                "toEntities": ["kube-apiserver"],
                "toPorts": [
                    {
                        "ports": [
                            {"port": "443", "protocol": "TCP"},
                            {"port": "6443", "protocol": "TCP"},
                        ]
                    }
                ],
            }
        ],
    }:
        raise ValueError("API-server policy is broader than the fixed entity/ports")


def prepare():
    active = (folder() / "active.json").read_bytes()
    candidate, _ = rendered(folder() / "values.yaml")
    previous = (folder() / "rendered.yaml").read_text()
    validate_delta(previous, candidate)
    value = {
        "schema_version": "klip-host-network-update.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "active_sha256": sha(active),
        "previous_rendered_sha256": sha(previous.encode()),
        "rendered_sha256": sha(candidate.encode()),
        "values_sha256": sha((folder() / "values.yaml").read_bytes()),
    }
    value["approval_sha256"] = sha(json.dumps(value, sort_keys=True).encode())
    root = folder() / "network-updates" / value["approval_sha256"]
    write_private(root / "plan.json", value)
    write_private(root / "previous-rendered.yaml", previous)
    write_private(root / "candidate.yaml", candidate)
    return value


def apply(approval):
    from cluster import FOLDER, checked, kubeconfigs, tunnel

    root = folder() / "network-updates" / approval
    plan = json.loads((root / "plan.json").read_text())
    if (
        approval
        != sha(
            json.dumps(
                {k: v for k, v in plan.items() if k != "approval_sha256"}, sort_keys=True
            ).encode()
        )
        or plan["source_sha256"] != source_digest()
        or plan["active_sha256"] != sha((folder() / "active.json").read_bytes())
        or plan["values_sha256"] != sha((folder() / "values.yaml").read_bytes())
        or plan["rendered_sha256"] != sha(rendered(folder() / "values.yaml")[0].encode())
        or plan["previous_rendered_sha256"] != sha((folder() / "rendered.yaml").read_bytes())
    ):
        raise ValueError("Network plan, source, active release or rendered scope changed")
    validate_delta(
        (root / "previous-rendered.yaml").read_text(), (root / "candidate.yaml").read_text()
    )
    result = root / "result.json"
    if result.exists():
        raise ValueError(
            "Existing network transition must be observed/reconciled, never repeated blindly"
        )
    record = {"approval_sha256": approval, "status": "running", "started_at": utc_now()}
    write_private(result, record)
    write_private(root / "previous-active.json", (folder() / "active.json").read_text())
    kubeconfigs()
    try:
        with (state_home() / "cluster/operator.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with tunnel():
                output = checked(
                    [
                        tool("helm"),
                        "upgrade",
                        "host-executor",
                        str(ROOT / "charts/klip-host-executor"),
                        "-n",
                        "platform-ops",
                        "--kubeconfig",
                        str(FOLDER / "operator.conf"),
                        "--values",
                        str(folder() / "values.yaml"),
                        "--atomic",
                        "--wait",
                        "--timeout",
                        "5m",
                        "--history-max",
                        "10",
                    ],
                    timeout=360,
                )
        write_private(root / "helm.log", output)
        active = json.loads((folder() / "active.json").read_text())
        record.update(status="deployed", deployed_at=utc_now())
        active.setdefault("updates", []).append({"kind": "api-server-network-policy", **record})
        write_private(folder() / "active.json", active)
        write_private(folder() / "rendered.yaml", (root / "candidate.yaml").read_text())
    except Exception as error:
        record.update(status="reconciliation_required", error_type=type(error).__name__)
        raise
    finally:
        write_private(result, record)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply"])
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    print(json.dumps(prepare() if args.action == "prepare" else apply(args.approve_plan), indent=2))
