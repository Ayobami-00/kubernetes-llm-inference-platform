"""Stage immutable public JFLEG inputs and plan only the user-retained A10 experiment."""

import argparse
import base64
import hashlib
import json
import re
import time
from pathlib import Path

import yaml
from common import ROOT, state_home, utc_now, write_private
from platform_contracts import InfrastructureInventory, PreparationManifest, WorkerGroupSpec
from retained_model import INSTANCE, NODE, checked_node, profile

ENDPOINT = "http://inference.llm-d.svc.cluster.local:8080/v1/chat/completions"
ARTIFACT_ROOT = "/artifacts/experiments/retained-a10"


def api_exec(program, *, data=None, args=()):
    from cluster import Host, kubectl, load_hosts

    pods = json.loads(
        kubectl(
            "-n",
            "klip",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=klip,app.kubernetes.io/component=api",
            "-o",
            "json",
        )
    )["items"]
    ready = [
        p
        for p in pods
        if not p["metadata"].get("deletionTimestamp")
        and any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in p.get("status", {}).get("conditions", [])
        )
    ]
    if not ready:
        raise ValueError("No healthy management API can access the shared volume")
    return Host(load_hosts()["cp-01"]).run(
        [
            "sudo",
            "kubectl",
            "--kubeconfig=/etc/kubernetes/admin.conf",
            "-n",
            "klip",
            "exec",
            "-i",
            ready[0]["metadata"]["name"],
            "-c",
            "api",
            "--",
            "python",
            "-c",
            program,
            *args,
        ],
        data=data,
        secret=True,
        timeout=120,
    )


def read_shared(path):
    if not path.startswith(ARTIFACT_ROOT + "/") or ".." in Path(path).parts:
        raise ValueError("Only retained A10 experiment artifacts may be read")
    return api_exec(
        "from pathlib import Path;import sys;print(Path(sys.argv[1]).read_text())", args=(path,)
    )


def stage(evidence, variant, identity):
    from cluster import Host, kubectl
    from workload_acceptance import images, preparation_job

    if variant not in {"baseline", "ngram"} or not re.fullmatch(
        r"a10-[a-z0-9]+(?:-[a-z0-9]+)*", identity
    ):
        raise ValueError("Explicit A10 experiment identity required")
    output = evidence / "runs" / identity
    if (output / "stage.json").exists():
        raise ValueError("Observe this existing immutable plan; do not refresh it in place")
    node = checked_node(evidence)
    template = yaml.safe_load((evidence / "prepare-inventory.yaml").read_text())
    host = Host(template["all"]["hosts"][NODE])
    boot = host.run(["cat", "/proc/sys/kernel/random/boot_id"]).strip()
    gpu = (
        host.run(
            [
                "nvidia-smi",
                "--query-gpu=uuid,name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ]
        )
        .strip()
        .split(", ")
    )
    ages = host.run(["sudo", "wg", "show", "wg0", "latest-handshakes"])
    if boot != node["boot_id"] or len(gpu) != 4 or gpu[1:] != ["NVIDIA A10", "23028", "580.105.08"]:
        raise ValueError("Retained host boot/GPU identity changed")
    times = [int(line.split()[1]) for line in ages.splitlines()]
    if len(times) != 6 or any(not t or time.time() - t > 180 for t in times):
        raise ValueError("Retained host mesh is not fresh for all six CPU peers")
    live = json.loads(kubectl("get", "node", NODE, "-o", "json"))
    cilium = json.loads(
        kubectl(
            "-n",
            "kube-system",
            "get",
            "pods",
            "-l",
            "k8s-app=cilium",
            "--field-selector",
            "spec.nodeName=" + NODE,
            "-o",
            "json",
        )
    )["items"]
    if len(cilium) != 1 or not any(
        c["type"] == "Ready" and c["status"] == "True" for c in cilium[0]["status"]["conditions"]
    ):
        raise ValueError("Retained Cilium agent is not ready")
    serving = profile(variant)
    deployment = json.loads(
        kubectl("-n", "llm-d", "get", "deployment", "writer-assist-a10", "-o", "json")
    )
    if (
        deployment["metadata"]["annotations"].get("klip.dev/serving-profile") != serving.ref
        or deployment["status"].get("readyReplicas") != 1
    ):
        raise ValueError("Requested experiment variant is not deployed and ready")
    observed = utc_now()
    spec = WorkerGroupSpec(
        id="latency-a10-retained",
        instance_type="gpu_1x_a10",
        region="us-east-1",
        hardware={"gpu_model": "A10", "gpu_count": 1, "vram_gib_per_gpu": 24.0},
        worker_class="latency",
        host_profile="lambda-stack-retained-a10-v1",
        capacity_lifecycle="externally_retained",
        ttl_minutes=None,
        notes=(
            "User-retained A10: no automatic lease, restart or termination. "
            "Allocation timestamp records first Kubernetes registration, not provider creation."
        ),
    )
    foundation = json.loads((state_home() / "reports/foundation.json").read_text())
    nodes = foundation["nodes"] + [
        {
            "id": NODE,
            "provider": "lambda",
            "provider_instance_id": INSTANCE,
            "managed_by": "external",
            "role": "gpu_worker",
            "public_ip": "129.153.2.170",
            "wireguard_ip": "10.200.0.67",
            "observed_at": observed,
            "access": {
                "ssh_user": "ubuntu",
                "ssh_credential_ref": "operator://lambda-host-ed25519",
            },
            "readiness": dict.fromkeys(
                ["host", "wireguard", "kubernetes", "cilium", "gpu_runtime"], True
            ),
            "kubernetes": {
                "name": NODE,
                "uid": node["uid"],
                "allocatable_gpu_count": 1,
                "allocated_gpu_count": 1,
            },
            "hardware": spec.hardware.model_dump(),
            "worker_group_ref": spec.ref,
        }
    ]
    root = ARTIFACT_ROOT + "/" + identity
    inventory = InfrastructureInventory(
        snapshot_id=identity + "-inventory",
        captured_at=observed,
        cluster={
            "id": "klip",
            "api_endpoint": foundation["allocation"]["api"]["endpoint"],
            "inference_endpoint": ENDPOINT,
            "ca_bundle_ref": "file://" + ARTIFACT_ROOT + "/ca.crt",
        },
        network=foundation["network"],
        nodes=nodes,
    )
    # The shared contract requires an artifact URI for the public CA.
    inv = inventory.model_dump(mode="json")
    inventory = InfrastructureInventory.model_validate(inv)
    status = {
        "worker_group_ref": spec.ref,
        "capacity_lifecycle": "externally_retained",
        "phase": "ready",
        "counts": {
            "desired_instances": 1,
            "allocated_instances": 1,
            "joined_nodes": 1,
            "gpu_ready_nodes": 1,
        },
        "members": [
            {
                "provider_instance_id": INSTANCE,
                "phase": "gpu_ready",
                "node_name": NODE,
                "node_uid": node["uid"],
                "gpu_observation": {
                    "observed_at": observed,
                    "allocatable_gpu_count": 1,
                    "devices": [
                        {
                            "index": 0,
                            "uuid": gpu[0],
                            "name": gpu[1],
                            "memory_total_mib": 23028,
                            "driver_version": gpu[3],
                        }
                    ],
                },
            }
        ],
        "observed_at": observed,
        "allocated_at": live["metadata"]["creationTimestamp"],
        "expires_at": None,
    }
    capacity = {
        "source": "Retained A10 operator proof " + identity,
        "provenance": "measured",
        "document": {
            "id": identity + "-capacity",
            "inventory_id": inventory.snapshot_id,
            "worker_groups": [
                {
                    "id": "writer-a10-retained-capacity",
                    "group": {
                        "fixture": False,
                        "spec": spec.model_dump(mode="json"),
                        "status": status,
                    },
                }
            ],
            "deployments": [
                {
                    "id": "writer-assist-a10",
                    "group_id": "writer-a10-retained-capacity",
                    "profile": serving.model_dump(mode="json"),
                    "kubernetes_uid": deployment["metadata"]["uid"],
                    "endpoint": ENDPOINT,
                }
            ],
        },
    }
    from platform_management.api.import_schemas import ExternalCapacityImport

    capacity = ExternalCapacityImport.model_validate(capacity).model_dump(mode="json")
    runner = images()["images"]["runner"]["image"]
    versions = {
        "runner_image": runner,
        "aiperf_version": "0.13.0",
        "evaluator_version": "0.1.0",
        "platform_versions": {
            "kubernetes": "1.36.5",
            "cilium": "1.20.4",
            "vllm": "0.32.0",
            "llm-d": "1.0.0",
            "model-pool-chart": "1.0.0",
            "serving-chart": "0.1.1",
        },
    }
    source = state_home() / "datasets/writer-a10-nonthinking/public/writer-assist/latest.json"
    manifest = PreparationManifest.model_validate_json(source.read_text())
    from workload_runner.artifacts import artifact_path

    entries = artifact_path(manifest.artifact.uri, manifest.artifact.sha256).read_bytes()
    prepared = json.loads(manifest.model_dump_json())
    prepared["artifact"]["uri"] = "file://" + ARTIFACT_ROOT + "/prepared/entries.jsonl"
    files = {
        ARTIFACT_ROOT + "/prepared/entries.jsonl": entries,
        ARTIFACT_ROOT + "/prepared/public/writer-assist/latest.json": (
            json.dumps(prepared) + "\n"
        ).encode(),
        ARTIFACT_ROOT + "/ca.crt": (state_home() / "cluster/ca.crt").read_bytes(),
        root + "/inventory.json": (inventory.model_dump_json(indent=2) + "\n").encode(),
        root + "/worker.yaml": yaml.safe_dump(spec.model_dump(mode="json")).encode(),
        root + "/profile.yaml": yaml.safe_dump(serving.model_dump(mode="json")).encode(),
        root + "/versions.json": json.dumps(versions).encode(),
        root + "/bindings.json": json.dumps(
            [
                {
                    "customer_ref": "writer-assist@1",
                    "serving_profile_ref": serving.ref,
                    "worker_group_ref": spec.ref,
                    "deployment_id": "writer-assist-a10",
                }
            ]
        ).encode(),
    }
    payload = {k: base64.b64encode(v).decode() for k, v in files.items()}
    api_exec(
        """import sys,json,base64
from pathlib import Path
from platform_management.integrations.job_runner import frozen_file
for name,value in json.load(sys.stdin).items():
 p=Path(name)
 if not p.is_relative_to('/artifacts/experiments/retained-a10'):
  raise ValueError('Outside experiment mount')
 frozen_file(p,base64.b64decode(value,validate=True))
print('staged immutable files')
""",
        data=json.dumps(payload),
    )
    job = preparation_job("a10a10a10002", runner)
    job["metadata"]["name"] = identity + "-plan"
    job["spec"]["activeDeadlineSeconds"] = 180
    job["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "workload-runner",
        "--repo",
        "/app",
        "--config",
        "worker-group=" + root + "/worker.yaml",
        "--config",
        "serving-profile=" + root + "/profile.yaml",
        "plan",
        "--scenario",
        "writer-assist-baseline",
        "--source",
        "public",
        "--prepared",
        ARTIFACT_ROOT + "/prepared",
        "--mode",
        "benchmark",
        "--serving-stack",
        "llm-d-vllm",
        "--endpoint",
        ENDPOINT,
        "--credential-env",
        "writer-assist=INFERENCE_TOKEN",
        "--bindings",
        root + "/bindings.json",
        "--inventory",
        root + "/inventory.json",
        "--versions",
        root + "/versions.json",
        "--output",
        root + "/plan",
    ]
    write_private(output / "plan-job.json", job)
    from cluster import load_hosts

    cp = Host(load_hosts()["cp-01"])
    cp.run(
        ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
        data=json.dumps(job),
    )
    write_private(
        output / "stage.json",
        {
            "identity": identity,
            "variant": variant,
            "node": node,
            "inventory": inv,
            "capacity": capacity,
            "files": {p: hashlib.sha256(v).hexdigest() for p, v in files.items()},
            "prepared_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "root": root,
        },
    )
    return {"identity": identity, "variant": variant, "plan_job": job["metadata"]["name"]}


def submit(evidence, identity):
    from platform_management.integrations.job_runner import frozen_file
    from workload_runner.management import ManagementClient, submit_bundle

    if not re.fullmatch(r"a10-[a-z0-9]+(?:-[a-z0-9]+)*", identity):
        raise ValueError("Explicit A10 experiment identity required")
    output = evidence / "runs" / identity
    staged = json.loads((output / "stage.json").read_text())
    if staged["identity"] != identity:
        raise ValueError("Saved experiment identity differs")
    bundle = json.loads(read_shared(staged["root"] + "/plan/management-bundle.json"))
    frozen_file(output / "management-bundle.json", (json.dumps(bundle, indent=2) + "\n").encode())
    client = ManagementClient(
        "https://management.klip-lab.click", ROOT / "dev/.secrets/admin_password"
    )
    try:
        for kind, payload in [
            (
                "inventory",
                {
                    "source": "Retained A10 native observations " + identity,
                    "provenance": "measured",
                    "document": staged["inventory"],
                },
            ),
            ("external-capacity", staged["capacity"]),
        ]:
            accepted = client.request("POST", "imports/" + kind, payload, identity + "-" + kind)
            receipt = client.wait(
                "imports/" + accepted["import_id"], "status", {"completed", "failed"}, timeout=90
            )
            write_private(output / (kind + "-receipt.json"), receipt)
            if receipt["status"] != "completed":
                raise ValueError("Retained capacity import failed; inspect saved receipt")
    finally:
        client.close()
    groups = {"latency-a10-retained@1": "writer-a10-retained-capacity"}
    write_private(output / "groups.json", groups)
    run = submit_bundle(
        output / "management-bundle.json",
        "https://management.klip-lab.click",
        ROOT / "dev/.secrets/admin_password",
        groups,
        identity,
        output / "submission",
    )
    return {
        "run_id": run.id,
        "status": run.status,
        "validity": run.result.validity if run.result else None,
        "slo_verdict": run.result.slo_verdict if run.result else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["stage", "submit"], default="stage")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--variant", choices=["baseline", "ngram"], default="baseline")
    parser.add_argument("--identity", required=True)
    args = parser.parse_args()
    result = (
        stage(args.evidence.resolve(), args.variant, args.identity)
        if args.action == "stage"
        else submit(args.evidence.resolve(), args.identity)
    )
    print(json.dumps(result, indent=2))
