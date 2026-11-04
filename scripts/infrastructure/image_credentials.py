"""Reviewed rolling kubelet credential-provider installation on protected CPU hosts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from datetime import UTC, datetime, timedelta

import yaml
from common import (
    CONFIG,
    PROFILES,
    Aws,
    private_dir,
    source_digest,
    state_home,
    utc_now,
    write_private,
)
from image_tools import CACHE, verified_binary
from smoke import wait_until

PATHS = [
    "/opt/klip/image-credentials/ecr-credential-provider",
    "/etc/kubernetes/image-credentials/provider.yaml",
    "/etc/systemd/system/kubelet.service.d/30-klip-image-credentials.conf",
]
BASE_ARGS = (
    "/usr/bin/kubelet $KUBELET_KUBECONFIG_ARGS $KUBELET_CONFIG_ARGS "
    "$KUBELET_KUBEADM_ARGS $KUBELET_EXTRA_ARGS"
)


def provider_config() -> dict:
    registry = f"{CONFIG['account_id']}.dkr.ecr.{CONFIG['region']}.amazonaws.com/klip/"
    return {
        "apiVersion": "kubelet.config.k8s.io/v1",
        "kind": "CredentialProviderConfig",
        "providers": [
            {
                "name": "ecr-credential-provider",
                "matchImages": [
                    registry + name
                    for name in ("management-backend", "management-web", "workload-runner")
                ],
                "defaultCacheDuration": "5m",
                "apiVersion": "credentialprovider.kubelet.k8s.io/v1",
                "env": [
                    {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
                    {"name": "AWS_SHARED_CREDENTIALS_FILE", "value": "/dev/null"},
                    {"name": "AWS_CONFIG_FILE", "value": "/dev/null"},
                    {"name": "AWS_REGION", "value": CONFIG["region"]},
                    {"name": "AWS_DEFAULT_REGION", "value": CONFIG["region"]},
                ],
                "tokenAttributes": {
                    "serviceAccountTokenAudience": "sts.amazonaws.com",
                    "cacheType": "Token",
                    "requireServiceAccount": True,
                    "requiredServiceAccountAnnotationKeys": ["eks.amazonaws.com/ecr-role-arn"],
                },
            }
        ],
    }


def assert_host(review: dict) -> None:
    if review["version"] != "Kubernetes v1.36.5" or review["argv"] not in {
        BASE_ARGS,
        BASE_ARGS + " $KUBELET_IMAGE_CREDENTIAL_ARGS",
    }:
        raise ValueError("Unexpected kubelet release/ExecStart; refuse to replace host overrides")
    allowed = {
        "/usr/lib/systemd/system/kubelet.service.d/10-kubeadm.conf",
        PATHS[2],
    }
    if not set(review["drop_ins"]) <= allowed or not review["drop_ins"]:
        raise ValueError("Unreviewed kubelet drop-in")
    config = yaml.safe_load(review["kubelet_config"])
    if (
        config.get("featureGates", {}).get("KubeletServiceAccountTokenForCredentialProviders")
        is False
    ):
        raise ValueError("The Pod-token credential-provider feature is disabled")
    for path, metadata in review["files"].items():
        if metadata and (metadata["uid"] != 0 or metadata["mode"] & 0o022):
            raise ValueError("Non-root or writable host configuration: " + path)


def remote_review(host) -> dict:
    program = r"""
import hashlib,json,stat,subprocess
from pathlib import Path
paths=json.loads(__import__('sys').stdin.read())
def describe(name):
 p=Path(name)
 if not p.exists() and not p.is_symlink(): return None
 s=p.lstat()
 if not stat.S_ISREG(s.st_mode): raise ValueError('Refuse linked/non-file host configuration')
 return {'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
         'uid':s.st_uid,'mode':stat.S_IMODE(s.st_mode)}
def command(*args): return subprocess.check_output(args,text=True).strip()
unit=command('systemctl','show','kubelet','--property=ExecStart','--value')
print(json.dumps({'version':command('/usr/bin/kubelet','--version'),
 'argv':unit.split('argv[]=',1)[1].split(' ;',1)[0],
 'drop_ins':command('systemctl','show','kubelet','--property=DropInPaths','--value').split(),
 'kubelet_config':Path('/var/lib/kubelet/config.yaml').read_text(),
 'files':{p:describe(p) for p in paths}}))
"""
    review = json.loads(
        host.run(
            ["sudo", "python3", "-c", program],
            data=json.dumps(
                [
                    *PATHS,
                    "/var/lib/kubelet/config.yaml",
                    "/var/lib/kubelet/kubeadm-flags.env",
                    "/etc/default/kubelet",
                ]
            ),
        )
    )
    assert_host(review)
    return review


def healthy_targets() -> dict:
    from cluster import FOLDER, kubectl

    expected = json.loads((FOLDER / "platform-placement.json").read_text())["nodes"]
    if set(expected) != {"platform-01", "platform-02", "platform-03"}:
        raise ValueError("Exactly three reviewed platform hosts are required")
    nodes = {
        n["metadata"]["name"]: n for n in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    }
    result = {}
    for name, declared in expected.items():
        node = nodes[name]
        if (
            node["metadata"]["uid"] != declared["uid"]
            or node["metadata"]["labels"].get("klip.dev.node-restriction.kubernetes.io/platform")
            != "true"
            or not any(
                c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]
            )
            or node["spec"].get("unschedulable")
        ):
            raise ValueError("Protected platform identity/readiness changed")
        lease = json.loads(kubectl("-n", "kube-node-lease", "get", "lease", name, "-o", "json"))
        if datetime.fromisoformat(lease["spec"]["renewTime"].replace("Z", "+00:00")) < datetime.now(
            UTC
        ) - timedelta(seconds=45):
            raise ValueError("Platform node lease is stale")
        result[name] = {"uid": node["metadata"]["uid"], "provider_id": node["spec"]["providerID"]}
    db = json.loads(kubectl("-n", "platform-data", "get", "cluster", "klip-db", "-o", "json"))
    if (
        db["status"].get("readyInstances") != 3
        or db["status"].get("instances") != 3
        or db["status"].get("phase") != "Cluster in healthy state"
    ):
        raise ValueError("All three database instances must be healthy before another host update")
    return result


def prepare() -> dict:
    from cluster import Host, load_hosts
    from platform_registry import verify

    verify()
    binary = verified_binary()
    targets = healthy_targets()
    hosts = load_hosts()
    for name in targets:
        targets[name]["before"] = remote_review(Host(hosts[name]))
    plan = {
        "schema_version": "klip-image-credentials-plan.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "targets": targets,
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "provider": provider_config(),
    }
    plan["approval_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    write_private(state_home() / "cluster/image-credentials/plan.json", plan)
    return plan


def apply(approval: str) -> dict:
    from cluster import FOLDER, Host, ansible, load_hosts

    folder = private_dir(FOLDER / "image-credentials")
    plan = json.loads((folder / "plan.json").read_text())
    signed = {k: v for k, v in plan.items() if k != "approval_sha256"}
    if (
        approval != plan["approval_sha256"]
        or approval != hashlib.sha256(json.dumps(signed, sort_keys=True).encode()).hexdigest()
        or plan["source_sha256"] != source_digest()
    ):
        raise ValueError("Implementation or reviewed host plan changed; prepare again")
    binary = verified_binary()
    if hashlib.sha256(binary).hexdigest() != plan["binary_sha256"]:
        raise ValueError("Reviewed helper changed")
    Aws(PROFILES["foundation"]).preflight("foundation")
    report_path = folder / (approval + ".json")
    report = (
        json.loads(report_path.read_text())
        if report_path.exists()
        else {
            "approval_sha256": approval,
            "started_at": utc_now(),
            "nodes": {},
            "status": "running",
        }
    )
    if report["status"] == "failed":
        raise ValueError(
            "This rollout failed; inspect retained observations before preparing a retry"
        )
    hosts = load_hosts()
    for name, target in plan["targets"].items():
        live = healthy_targets()
        if any(
            live[n] != {k: v for k, v in t.items() if k != "before"}
            for n, t in plan["targets"].items()
        ):
            raise ValueError("Platform target identity changed")
        host = Host(hosts[name])
        if name in report["nodes"]:
            if remote_review(host) != report["nodes"][name]["after"]:
                raise ValueError("An installed host changed since this rollout")
            continue
        if remote_review(host) != target["before"]:
            raise ValueError("Host configuration changed after review")
        report["current_node"] = name
        write_private(report_path, report)
        inventory = folder / (name + ".yaml")
        write_private(
            inventory,
            yaml.safe_dump(
                {
                    "all": {
                        "children": {
                            "image_credentials": {"hosts": {name: hosts[name]}},
                        }
                    }
                }
            ),
        )
        try:
            ansible(
                "image-credentials",
                inventory=inventory,
                extra={
                    "klip_image_credentials_binary": str(CACHE),
                    "klip_image_credentials_install_paths": PATHS,
                    "klip_image_credentials_provider_yaml": yaml.safe_dump(plan["provider"]),
                },
                log_path=folder / (name + ".ansible.log"),
            )
            after = remote_review(host)
            if (
                after["files"][PATHS[0]]["sha256"] != plan["binary_sha256"]
                or after["files"][PATHS[1]]["sha256"]
                != hashlib.sha256(yaml.safe_dump(plan["provider"]).encode()).hexdigest()
                or after["argv"] != BASE_ARGS + " $KUBELET_IMAGE_CREDENTIAL_ARGS"
                or any(
                    after["files"][p] != target["before"]["files"][p]
                    for p in (
                        "/var/lib/kubelet/config.yaml",
                        "/var/lib/kubelet/kubeadm-flags.env",
                        "/etc/default/kubelet",
                    )
                )
            ):
                raise ValueError("Installed provider or preserved kubeadm configuration differs")
            wait_until(healthy_targets, "unchanged healthy platform/database", timeout=120)
            report["nodes"][name] = {"verified_at": utc_now(), "after": after}
            write_private(report_path, report)
        except Exception:
            report.update(status="failed", failed_at=utc_now())
            write_private(report_path, report)
            raise
    report.update(status="completed", completed_at=utc_now())
    report.pop("current_node", None)
    write_private(report_path, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply"])
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    folder = private_dir(state_home() / "cluster")
    with (folder / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(
            json.dumps(
                prepare() if args.action == "prepare" else apply(args.approve_plan), indent=2
            )
        )
