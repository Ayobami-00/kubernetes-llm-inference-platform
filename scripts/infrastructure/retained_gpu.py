"""Operator-only onboarding of a user-retained A10. No provider mutation interface.

Preparation adds an isolated CRI service; joining runs under the live executor's
two ownership locks. Neither action rents, leases, reboots or removes a VM.
"""

import argparse
import hashlib
import json
import shlex
from pathlib import Path

import yaml
from common import ROOT, state_home, utc_now, write_private

INSTANCE = "8f2e29b7279b42148ffaaed4a4dcdd8e"
NAME = "writer-a10-retained"
SOCKET = "unix:///run/klip-containerd/containerd.sock"


def inputs(evidence):
    inspected = json.loads((evidence / "inspection.json").read_text())
    runtime = json.loads((evidence / "runtime-inspection.json").read_text())
    if (
        inspected["instance_id"] != INSTANCE
        or inspected["ownership"] != "external-user-retained"
        or inspected["automatic_termination"] is not False
        or inspected["automatic_restart"] is not False
        or inspected["lease"] is not None
        or inspected["trust_source"] != "provider-authenticated HTTPS Jupyter execution"
        or inspected["public_ip"] != "129.153.2.170"
        or inspected["inspection"]["gpu"]["stdout"].strip()
        != "NVIDIA A10, 580.105.08, 23028 MiB, 8.6"
        or inspected["inspection"]["gpu_processes"]["stdout"].strip()
        or inspected["inspection"]["containerd"]["stdout"].split()[2] != "v2.4.2"
    ):
        raise ValueError("Retained A10 identity or ownership differs from the inspected host")
    # Public host-key evidence must agree with the independent SSH pin.
    key = " ".join(inspected["inspection"]["ssh_host_key"].split()[:2])
    if (evidence / "known_hosts").read_text().strip() != inspected["public_ip"] + " " + key:
        raise ValueError("SSH pin differs from provider-authenticated evidence")
    allocation = json.loads((state_home() / "reports/foundation.json").read_text())["allocation"]
    from cluster_config import node_configuration

    node = node_configuration(
        NAME,
        allocation,
        {
            "instance_id": INSTANCE,
            "public_ip": inspected["public_ip"],
            "private_ip": "10.19.94.24",
            "wireguard_ip": "10.200.0.67",
        },
        role="worker",
        provider="lambda",
    )
    node.update(quarantine=True, cri_socket=SOCKET)
    retained = {
        "instance_id": INSTANCE,
        "ownership": "external-user-retained",
        "automatic_termination": False,
        "automatic_restart": False,
        "lease": None,
        "boot_id": runtime["boot_id"],
        "gpu": inspected["inspection"]["gpu"]["stdout"].strip(),
    }
    return {
        "retained": retained,
        "node": node,
        "trusted_host_key": key,
        "public_ip": inspected["public_ip"],
    }


def prepare(evidence):
    from cluster import ansible

    value = inputs(evidence)
    fields = {
        "ansible_host": value["public_ip"],
        "ansible_user": "ubuntu",
        "ansible_ssh_private_key_file": str(state_home() / "credentials/lambda-host-ed25519"),
        "ansible_ssh_common_args": "-o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
        "-o UserKnownHostsFile=" + shlex.quote(str((evidence / "known_hosts").resolve())),
        "klip_node": value["node"],
        "klip_retained": value["retained"],
    }
    inventory = evidence / "prepare-inventory.yaml"
    write_private(
        inventory,
        yaml.safe_dump(
            {
                "all": {
                    "vars": {"klip_repository": str(ROOT)},
                    "hosts": {NAME: fields},
                }
            }
        ),
    )
    ansible("retained-gpu", inventory=inventory, log_path=evidence / "prepare.log")
    from cluster import Host

    value["public_wireguard_key"] = Host(fields).agent("wg-key")["public_key"]
    value["prepared_at"] = utc_now()
    write_private(evidence / "prepared.json", value)
    return {"status": "prepared", "instance_id": INSTANCE, "vm_restarted": False}


# Executed only inside an operator-created, never-retrying Job using the current
# executor image and custody. Inputs contain public identity/configuration only.
JOIN_PROGRAM = Path(__file__).with_name("retained_gpu_native.py").read_text()


def native_program(previous=None):
    import re

    if previous is not None and not re.fullmatch(r"[a-f0-9]{64}", previous):
        raise ValueError("Exact prior failed result checksum required")
    return JOIN_PROGRAM


def join_job(value, deployment, previous=None):
    """Use existing custody in an isolated, single-attempt operator Job."""
    from host_executor_release import bootstrap_job

    program = native_program(previous)
    binding = hashlib.sha256(
        json.dumps(
            {"input": value, "program": program, "previous": previous}, sort_keys=True
        ).encode()
    ).hexdigest()
    job = bootstrap_job({"approval_sha256": binding}, [deployment])
    name = "retained-a10-" + binding[:20]
    job["metadata"]["name"] = name
    pod = job["spec"]["template"]["spec"]
    labels = job["spec"]["template"]["metadata"]["labels"]
    labels["klip.dev/host-task"] = name
    labels["app.kubernetes.io/component"] = "retained-join"
    container = pod["containers"][0]
    container["command"] = ["python", "-c", program]
    pod["volumes"].append({"name": "ansible", "emptyDir": {"sizeLimit": "128Mi"}})
    container["volumeMounts"].append({"name": "ansible", "mountPath": "/app/artifacts/ansible"})
    container["env"] += [
        {"name": "KLIP_RETAINED_INPUT", "value": json.dumps(value, sort_keys=True)},
        {"name": "KLIP_RETAINED_ATTEMPT", "value": binding},
        {"name": "KLIP_RETAINED_PREVIOUS", "value": previous or ""},
        {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        {
            "name": "JOB_UID",
            "valueFrom": {
                "fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/controller-uid']"}
            },
        },
    ]
    return job


def operator_preinstalled(evidence):
    """Keep Helm's complete values; change only driver/toolkit ownership."""
    from cluster import FOLDER, checked, kubeconfigs, kubectl, tunnel
    from common import tool
    from gpu_operator import CACHE, settings

    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    if any(
        n["metadata"]["name"]
        not in {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}
        for n in nodes
    ):
        raise ValueError("Preinstalled overlay requires no joined GPU workers")
    kubeconfigs()
    with tunnel():
        arguments = [tool("helm"), "--kubeconfig", str(FOLDER / "operator.conf")]
        values = json.loads(
            checked([*arguments, "get", "values", "gpu-operator", "-n", "gpu-system", "-o", "json"])
        )
        overlay = yaml.safe_load(
            (ROOT / "deploy/environments/lab/gpu-retained-values.yaml").read_text()
        )
        for key in ("driver", "toolkit"):
            values[key].update(overlay[key])
        path = evidence / "gpu-operator-values.yaml"
        write_private(path, yaml.safe_dump(values))
        checked(
            [
                *arguments,
                "upgrade",
                "gpu-operator",
                str(CACHE / settings()["chart"]["file"]),
                "-n",
                "gpu-system",
                "--values",
                str(path),
                "--atomic",
                "--wait",
                "--timeout",
                "5m",
                "--history-max",
                "5",
            ],
            timeout=360,
        )
    policy = json.loads(kubectl("get", "clusterpolicy", "cluster-policy", "-o", "json"))
    if policy["spec"]["driver"]["enabled"] or policy["spec"]["toolkit"]["enabled"]:
        raise ValueError("Operator did not retain preinstalled driver/toolkit ownership")
    result = {
        "policy_uid": policy["metadata"]["uid"],
        "generation": policy["metadata"]["generation"],
        "driver_enabled": False,
        "toolkit_enabled": False,
        "observed_at": utc_now(),
    }
    write_private(evidence / "gpu-operator-receipt.json", result)
    return result


def image_credentials(evidence):
    """Reuse the reviewed Pod-bound provider with model-repository-only matching."""
    from cluster import Host, ansible
    from image_credentials import PATHS, assert_host, provider_config, remote_review
    from image_tools import CACHE, verified_binary

    verified_binary()
    inventory = yaml.safe_load((evidence / "prepare-inventory.yaml").read_text())
    fields = inventory["all"]["hosts"][NAME]
    host = Host(fields)
    before = remote_review(host)
    assert_host(before)
    provider = provider_config()
    provider["providers"][0]["matchImages"] = [
        "127811062476.dkr.ecr.eu-west-2.amazonaws.com/klip/model-serving"
    ]
    path = evidence / "image-credentials-inventory.yaml"
    write_private(
        path,
        yaml.safe_dump({"all": {"children": {"image_credentials": {"hosts": {NAME: fields}}}}}),
    )
    ansible(
        "image-credentials",
        inventory=path,
        extra={
            "klip_image_credentials_binary": str(CACHE),
            "klip_image_credentials_install_paths": PATHS,
            "klip_image_credentials_provider_yaml": yaml.safe_dump(provider),
        },
        log_path=evidence / "image-credentials.log",
    )
    after = remote_review(host)
    assert_host(after)
    if (
        host.run(["cat", "/proc/sys/kernel/random/boot_id"]).strip()
        != inputs(evidence)["retained"]["boot_id"]
    ):
        raise ValueError("Retained VM boot identity changed")
    result = {
        "before": before,
        "after": after,
        "provider": provider,
        "observed_at": utc_now(),
        "vm_restarted": False,
    }
    write_private(evidence / "image-credentials.json", result)
    return {"status": "configured", "match_images": provider["providers"][0]["matchImages"]}


def join(evidence, previous=None):
    from cluster import Host, kubectl, load_hosts

    value = json.loads((evidence / "prepared.json").read_text())
    expected = inputs(evidence)
    if {k: value[k] for k in expected} != expected:
        raise ValueError("Prepared ownership or host inputs changed")
    policy = json.loads(kubectl("get", "clusterpolicy", "cluster-policy", "-o", "json"))
    if policy["spec"]["driver"].get("enabled", True) or policy["spec"]["toolkit"].get(
        "enabled", True
    ):
        raise ValueError("Apply the retained-host GPU Operator overlay before joining")
    deployment = json.loads(
        kubectl("-n", "platform-ops", "get", "deployment", "host-executor", "-o", "json")
    )
    job = join_job(value, deployment, previous)
    write_private(evidence / "join-job.json", job)
    old = kubectl(
        "-n",
        "platform-ops",
        "get",
        "job",
        job["metadata"]["name"],
        "--ignore-not-found",
        "-o",
        "json",
    )
    if old.strip():
        existing = json.loads(old)
        if (
            existing["metadata"]["annotations"].get("klip.dev/release-sha256")
            != job["metadata"]["annotations"]["klip.dev/release-sha256"]
        ):
            raise ValueError("Existing retained Job binding differs")
        return {"job": existing["metadata"]["name"], "uid": existing["metadata"]["uid"]}
    cp = Host(load_hosts()["cp-01"])
    actual = json.loads(
        cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "create",
                "-f",
                "-",
                "-o",
                "json",
            ],
            data=json.dumps(job),
        )
    )
    receipt = {"job": actual["metadata"]["name"], "uid": actual["metadata"]["uid"]}
    write_private(evidence / "join-receipt.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "operator-preinstalled", "image-credentials", "join"]
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--previous-failed-result")
    args = parser.parse_args()
    action = {
        "prepare": prepare,
        "operator-preinstalled": operator_preinstalled,
        "image-credentials": image_credentials,
        "join": join,
    }[args.action]
    result = (
        join(args.evidence.resolve(), args.previous_failed_result)
        if args.action == "join"
        else action(args.evidence.resolve())
    )
    print(json.dumps(result, indent=2))
