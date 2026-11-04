"""One native probe attempt for the retained preinstalled-driver A10."""

import fcntl
import hashlib
import json
import os
import traceback
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from cluster import Host, load_hosts
from common import utc_now, write_private
from platform_management.host_executor.inspection import checked_node, patch_node
from platform_management.host_executor.worker import scope
from platform_management.integrations.job_runner import frozen_file


def require(condition, message):
    if not condition:
        raise ValueError(message)


root = Path("/executor/state")
value = json.loads(os.environ["KLIP_RETAINED_INPUT"])
binding = os.environ["KLIP_RETAINED_ATTEMPT"]
require(value["retained"]["instance_id"] == "8f2e29b7279b42148ffaaed4a4dcdd8e", "Wrong VM")
require(value["retained"]["lease"] is None, "Retained VM cannot have a lease")
directory = root / "retained" / value["retained"]["instance_id"] / "probes" / binding
directory.mkdir(parents=True, exist_ok=True)
frozen_file(
    directory / "owner.json",
    json.dumps(
        {"job_uid": os.environ["JOB_UID"], "pod_uid": os.environ["POD_UID"]}, sort_keys=True
    ).encode(),
)
with ExitStack() as stack:
    for name in ("dispatch.lock", "cluster/operator.lock"):
        lock = stack.enter_context((root / name).open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result_path = directory / "result.json"
    require(not result_path.exists(), "Existing probe attempt must not restart")
    result = {"status": "running", "started_at": utc_now(), "stage": "validating"}
    write_private(result_path, result)
    try:
        inputs, registry, cp = scope(root)
        from cluster import load_hosts

        host = Host(load_hosts()[value["node"]["name"]])
        require(
            host.run(["cat", "/proc/sys/kernel/random/boot_id"]).strip()
            == value["retained"]["boot_id"],
            "VM boot changed",
        )
        task = SimpleNamespace(
            id="host-" + binding[:24],
            node_name=value["node"]["name"],
            node_uid=value["node_uid"],
            provider_instance_id=value["retained"]["instance_id"],
            wireguard_ip=value["node"]["wireguard_ip"],
            deadline=datetime.now(UTC) + timedelta(minutes=8),
        )
        command = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf"]
        namespace = json.loads(
            cp.run([*command, "get", "namespace", "klip-acceptance", "-o", "json"])
        )
        require(
            namespace["metadata"]["uid"] == value["acceptance_namespace_uid"],
            "Probe namespace changed",
        )
        node = json.loads(cp.run([*command, "get", "node", task.node_name, "-o", "json"]))
        checked_node(task, node, require_gpu=True)
        patch_node(cp, task, node)
        module = {"__name__": "retained_probe_module"}
        source = value["probe_source"]
        require(
            hashlib.sha256(source.encode()).hexdigest() == value["probe_source_sha256"],
            "Probe source changed",
        )
        exec(compile(source, "retained-probes", "exec"), module)
        result.update(stage="cuda-network-dns-mtu")
        write_private(result_path, result)
        evidence = module["execute"](
            root, task, cp, set(inputs["cpu_peers"]), host, expected_driver="580.105.08"
        )
        require(len(evidence["connections"]) == 49, "Incomplete Pod/Service connectivity proof")
        require(
            host.run(["cat", "/proc/sys/kernel/random/boot_id"]).strip()
            == value["retained"]["boot_id"],
            "VM boot changed",
        )
        node = json.loads(cp.run([*command, "get", "node", task.node_name, "-o", "json"]))
        promoted = patch_node(cp, task, node, promote=True)
        result.update(
            status="completed",
            stage="gpu-ready",
            node_uid=promoted["metadata"]["uid"],
            connections=len(evidence["connections"]),
            hardware=evidence["hardware"],
            mtu=evidence["mtu"],
            vm_restarted=False,
            completed_at=utc_now(),
        )
    except Exception as error:
        result.update(
            status="failed",
            stage="requires-observation",
            error_type=type(error).__name__,
            frames=[
                {"file": x.filename.rsplit("/", 1)[-1], "line": x.lineno}
                for x in traceback.extract_tb(error.__traceback__)
            ],
            completed_at=utc_now(),
        )
        raise
    finally:
        write_private(result_path, result)
