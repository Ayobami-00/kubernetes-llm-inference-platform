"""One drained, UID-bound persistent CPU worker per reviewed Terraform resize."""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from common import (
    CONFIG,
    PROFILES,
    ROOT,
    Aws,
    private_dir,
    source_digest,
    state_home,
    tf,
    utc_now,
    write_private,
)

NODES = {"platform-01", "platform-02", "platform-03"}
PROTECTED = "klip.dev.node-restriction.kubernetes.io/platform"


def folder():
    return private_dir(state_home() / "cluster/platform-resize")


def active():
    return json.loads((folder() / "active.json").read_text())


def save(record):
    write_private(folder() / "active.json", record)
    if record.get("plan_sha256"):
        write_private(folder() / record["plan_sha256"] / "resize.json", record)


def desired_type():
    desired = json.loads((ROOT / "infra/terraform/platform.json").read_text())["instance_type"]
    envelope = json.loads((ROOT / "deploy/platform-capacity-envelope.json").read_text())
    if desired != envelope["desired_instance_type"] or desired != "m7i.2xlarge":
        raise ValueError("This procedure requires its reviewed 8-vCPU/32-GiB envelope")
    return desired


def node(name):
    from cluster import kubectl

    if name not in NODES:
        raise ValueError("Only the three protected persistent CPU workers can be resized")
    return json.loads(kubectl("get", "node", name, "-o", "json"))


def ready(value):
    return any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in value.get("status", {}).get("conditions", [])
    )


def allocation():
    from platform_workers import validate_allocation, verify_instances

    value = json.loads(tf("platform", "output", "-json", "platform_manifest"))
    # Materialize legacy homogeneous observations into explicit per-node sizes.
    # The observed size is preserved, not replaced by the new desired default.
    previous_default = value["instance_type"]
    value["nodes"] = {
        name: {**item, "instance_type": item.get("instance_type", previous_default)}
        for name, item in value["nodes"].items()
    }
    value["instance_type"] = desired_type()
    validate_allocation(value)
    aws = Aws(PROFILES["platform"])
    aws.preflight("platform")
    instances = [
        i
        for r in aws.call(
            "ec2",
            "describe-instances",
            "--instance-ids",
            *[x["instance_id"] for x in value["nodes"].values()],
        )["Reservations"]
        for i in r["Instances"]
    ]
    interfaces = aws.call(
        "ec2",
        "describe-network-interfaces",
        "--network-interface-ids",
        *[x["eni_id"] for x in value["nodes"].values()],
    )["NetworkInterfaces"]
    volumes = aws.call(
        "ec2",
        "describe-volumes",
        "--volume-ids",
        *[x["root_volume_id"] for x in value["nodes"].values()],
    )["Volumes"]
    verify_instances(value, instances, interfaces, volumes)
    for instance in instances:
        tags = {x["Key"]: x["Value"] for x in instance["Tags"]}
        if (
            tags.get("Environment") != CONFIG["environment"]
            or tags.get("Cluster") != CONFIG["cluster_id"]
        ):
            raise ValueError("Resize cannot target a foreign environment/cluster")
    return value


def resize_change(document, selected=None):
    changes = []
    for resource in document.get("resource_changes", []):
        change = resource["change"]
        if resource.get("mode") == "data" or change["actions"] == ["no-op"]:
            continue
        if (
            resource["type"] != "aws_instance"
            or change["actions"] != ["update"]
            or resource["address"]
            not in {f'module.compute.aws_instance.node["{n}"]' for n in NODES}
        ):
            raise ValueError("A resize plan may update only one existing platform instance")
        before, after = change["before"], change["after"]
        if before.get("instance_type") == after.get("instance_type"):
            raise ValueError("Resize plan contains an unrelated instance update")
        for key in set(before) | set(after):
            if key in {"instance_type", "cpu_options"}:
                continue
            if (
                key in {"public_ip", "public_dns"}
                and change.get("after_unknown", {}).get(key) is True
            ):
                # The provider recomputes these on stop/start. The exact retained
                # EIP/ENI bindings are separately verified before and after.
                continue
            if before.get(key) != after.get(key):
                raise ValueError(
                    "Resize changes a field other than size/derived CPU options: " + key
                )
        if after["instance_type"] != desired_type():
            raise ValueError("Resize must use the explicitly reviewed versioned CPU size")
        options = after.get("cpu_options", [])
        if (
            len(options) != 1
            or options[0].get("core_count") != 4
            or options[0].get("threads_per_core") != 2
        ):
            raise ValueError("The reviewed size must expose four cores and two threads per core")
        for key in set((before.get("cpu_options") or [{}])[0]) | set(options[0]):
            if key not in {"core_count", "threads_per_core"} and (
                before.get("cpu_options") or [{}]
            )[0].get(key) != options[0].get(key):
                raise ValueError("Unreviewed CPU option change")
        changes.append(resource)
    if len(changes) != 1 or (
        selected and changes[0]["address"] != f'module.compute.aws_instance.node["{selected}"]'
    ):
        raise ValueError("Exactly one selected CPU worker must change at a time")
    return changes[0]


def workload_pods(name):
    from cluster import kubectl

    values = json.loads(
        kubectl("get", "pods", "-A", "--field-selector", f"spec.nodeName={name}", "-o", "json")
    )["items"]
    result = []
    for p in values:
        if p["status"]["phase"] in {"Succeeded", "Failed"}:
            continue
        owners = p["metadata"].get("ownerReferences", [])
        if any(x.get("controller") and x["kind"] == "DaemonSet" for x in owners):
            continue
        if p["metadata"].get("namespace") not in {
            "klip",
            "cert-manager",
            "cnpg-system",
            "identity-system",
            "platform-data",
            "kube-system",
            "observability",
        }:
            raise ValueError("Unreviewed workload on selected maintenance host")
        if any(x.get("controller") and x["kind"] == "Job" for x in owners):
            raise ValueError("Maintenance cannot interrupt an active runner or migration Job")
        result.append(p)
    return result


def no_active_jobs():
    from cluster import kubectl

    pods = json.loads(kubectl("-n", "klip", "get", "pods", "-o", "json"))["items"]
    if any(
        p["status"]["phase"] not in {"Succeeded", "Failed"}
        and any(
            o.get("controller") and o["kind"] == "Job"
            for o in p["metadata"].get("ownerReferences", [])
        )
        for p in pods
    ):
        raise ValueError("Maintenance requires no active runner or migration Jobs")


def assert_survivors(record):
    from cluster import kubectl
    from database_failover import observe

    for name, uid in record["node_uids"].items():
        observed = node(name)
        if (
            observed["metadata"]["uid"] != uid
            or observed["metadata"]["labels"].get(PROTECTED) != "true"
            or not ready(observed)
        ):
            raise ValueError("Platform identity or readiness changed during maintenance")
        lease = json.loads(kubectl("-n", "kube-node-lease", "get", "lease", name, "-o", "json"))
        renewed = datetime.fromisoformat(lease["spec"]["renewTime"].replace("Z", "+00:00"))
        if renewed < datetime.now(UTC) - timedelta(seconds=45):
            raise ValueError("A surviving platform Node lease is stale")
    db, pods = observe()
    if (
        db["metadata"]["uid"] != record["database_uid"]
        or db["status"].get("readyInstances", 0) < 2
        or db["spec"]["postgresql"]["synchronous"]
        != {"dataDurability": "required", "method": "any", "number": 1, "failoverQuorum": True}
    ):
        raise ValueError("Two durable database replicas with the original quorum are required")
    healthy = [p for p in pods if p["spec"].get("nodeName") != record["node"] and ready(p)]
    if len(healthy) < 2 or db["status"]["currentPrimary"] not in {
        p["metadata"]["name"] for p in healthy
    }:
        raise ValueError("The primary and synchronous standby must survive off the drained host")
    deployments = json.loads(
        kubectl(
            "-n", "klip", "get", "deployments", "klip-api", "klip-worker", "klip-web", "-o", "json"
        )
    )["items"]
    if len(deployments) != 3 or any(
        d["status"].get("readyReplicas", 0) < 2
        or d["status"].get("observedGeneration") != d["metadata"]["generation"]
        for d in deployments
    ):
        raise ValueError("All management roles must recover their two replicas before resizing")


def verify_for_apply(receipt, document):
    """Called by generic apply too, so it cannot silently resize several hosts."""
    resized = [
        x
        for x in document.get("resource_changes", [])
        if x["type"] == "aws_instance"
        and x["change"]["actions"] == ["update"]
        and x["change"]["before"].get("instance_type") != x["change"]["after"].get("instance_type")
    ]
    if not resized:
        return
    change = resize_change(document)
    record = active()
    if (
        record.get("plan_sha256") != receipt["plan_sha256"]
        or record.get("stage") != "applying"
        or record["instance_id"] != change["change"]["before"]["id"]
    ):
        raise ValueError("A matching drained maintenance attempt is required before CPU resize")
    drained = datetime.fromisoformat(record["drained_at"])
    if not datetime.now(UTC) - timedelta(minutes=15) <= drained <= datetime.now(UTC):
        raise ValueError("Maintenance drain proof expired")
    if not node(record["node"])["spec"].get("unschedulable") or workload_pods(record["node"]):
        raise ValueError("The exact selected worker must remain cordoned and drained")
    no_active_jobs()
    assert_survivors(record)
    from platform_frontend import require_detached

    require_detached(record)


def prepare(name, backup_operation):
    from database_failover import backup_reference, observe, primary_target
    from image_credentials import healthy_targets
    from manage import create_plan

    if name not in NODES:
        raise ValueError("Select a persistent platform CPU node")
    current = folder() / "active.json"
    if current.exists() and active()["stage"] not in {"completed", "failed"}:
        if active()["node"] == name and active()["source_sha256"] == source_digest():
            return active()
        raise ValueError("Finish or reconcile the active maintenance attempt first")
    targets = healthy_targets()
    no_active_jobs()
    db, pods = observe()
    primary_target(db, pods, targets)
    backup = backup_reference(backup_operation, db["metadata"]["uid"])
    observed = allocation()
    item = observed["nodes"][name]
    if not targets[name]["provider_id"].endswith("/" + item["instance_id"]):
        raise ValueError(
            "The protected Kubernetes identity does not identify the selected EC2 host"
        )
    if item["instance_type"] == desired_type():
        raise ValueError(
            "Selected worker already has the desired type; observe its original attempt"
        )
    for host in NODES:
        if node(host)["spec"].get("unschedulable"):
            raise ValueError("No worker may already be cordoned before maintenance")
    affected = workload_pods(name)
    from platform_frontend import prepare as prepare_frontend

    frontend = prepare_frontend()
    overrides = {
        host: value["instance_type"]
        for host, value in observed["nodes"].items()
        if host != name and value["instance_type"] != desired_type()
    }
    receipt = create_plan("platform", platform_overrides=overrides)
    document = json.loads(tf("platform", "show", "-json", receipt["plan_path"]))
    resize_change(document, name)
    destination = private_dir(folder() / receipt["plan_sha256"])
    for key in ("plan_path", "variables_path"):
        source = Path(receipt[key])
        shutil.copyfile(source, destination / source.name)
        (destination / source.name).chmod(0o600)
    write_private(destination / "terraform-receipt.json", receipt)
    record = {
        "stage": "prepared",
        "prepared_at": utc_now(),
        "node": name,
        "source_sha256": source_digest(),
        "plan_sha256": receipt["plan_sha256"],
        "instance_id": item["instance_id"],
        "root_volume_id": item["root_volume_id"],
        "previous_type": item["instance_type"],
        "desired_type": desired_type(),
        "node_uids": {host: value["uid"] for host, value in targets.items()},
        "database_uid": db["metadata"]["uid"],
        "frontend": frontend,
        "backup": backup,
        "drain_pods": [
            {
                "namespace": p["metadata"]["namespace"],
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
            }
            for p in affected
        ],
        "cost_review": json.loads((ROOT / "deploy/platform-capacity-envelope.json").read_text())[
            "price_review"
        ],
    }
    save(record)
    return record


def set_cordon(record, value, *, observed_type=None):
    from cluster import kubectl

    observed = node(record["node"])
    if (
        observed["metadata"]["uid"] != record["node_uids"][record["node"]]
        or observed["metadata"]["labels"].get(PROTECTED) != "true"
    ):
        raise ValueError("Refuse to change a replacement or unprotected Node")
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": observed["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": observed["metadata"]["resourceVersion"],
        },
        {"op": "add", "path": "/spec/unschedulable", "value": value},
    ]
    if value is False:
        patch.append(
            {
                "op": "add",
                "path": "/metadata/labels/node.kubernetes.io~1instance-type",
                "value": observed_type or record["desired_type"],
            }
        )
    kubectl("patch", "node", record["node"], "--type=json", "-p", json.dumps(patch))


def sufficient_capacity(value):
    resources = value["status"]["allocatable"]
    cpu = resources["cpu"]
    cpu = float(cpu[:-1]) / 1000 if cpu.endswith("m") else float(cpu)
    memory = resources["memory"]
    factors = {"Ki": 1 / 1024**2, "Mi": 1 / 1024, "Gi": 1}
    if memory[-2:] not in factors:
        raise ValueError("Unexpected Node memory quantity")
    memory_gib = float(memory[:-2]) * factors[memory[-2:]]
    return value["status"]["capacity"]["cpu"] == "8" and cpu >= 7.5 and memory_gib >= 29


def verify_capacity():
    """Check actual capacity after maintenance or a fresh desired-size rebuild."""
    from image_credentials import healthy_targets

    targets = healthy_targets()
    declared = allocation()  # Rechecks the exact instances, ENIs and root disks.
    observations = {}
    for name, item in declared["nodes"].items():
        value = node(name)
        if (
            value["metadata"]["uid"] != targets[name]["uid"]
            or value["metadata"]["labels"].get(PROTECTED) != "true"
            or value["metadata"]["labels"].get("node.kubernetes.io/instance-type") != desired_type()
            or not value["spec"].get("providerID", "").endswith("/" + item["instance_id"])
            or value["spec"].get("unschedulable")
            or item["instance_type"] != desired_type()
            or not ready(value)
            or not sufficient_capacity(value)
        ):
            raise ValueError("Every original protected worker must expose the desired capacity")
        observations[name] = {
            "uid": value["metadata"]["uid"],
            "instance_id": item["instance_id"],
            "instance_type": item["instance_type"],
            "root_volume_id": item["root_volume_id"],
            "availability_zone": item["availability_zone"],
            "capacity": value["status"]["capacity"],
            "allocatable": value["status"]["allocatable"],
        }
    statuses = Aws(PROFILES["platform"]).call(
        "ec2",
        "describe-instance-status",
        "--instance-ids",
        *[item["instance_id"] for item in declared["nodes"].values()],
        "--include-all-instances",
    )["InstanceStatuses"]
    if (
        len(statuses) != 3
        or {item["InstanceId"] for item in statuses}
        != {item["instance_id"] for item in declared["nodes"].values()}
        or any(
            item["InstanceState"]["Name"] != "running"
            or item["SystemStatus"]["Status"] != "ok"
            or item["InstanceStatus"]["Status"] != "ok"
            for item in statuses
        )
    ):
        raise ValueError("All three exact EC2 workers must pass system and guest checks")
    report = {
        "schema_version": "klip-platform-capacity-verification.v1",
        "verified_at": utc_now(),
        "source_sha256": source_digest(),
        "nodes": observations,
        "limitations": [
            "Verifies actual worker capacity and current database readiness only",
            "Telemetry requests and unplanned worker-loss recovery need separate validation",
        ],
    }
    write_private(folder() / "capacity-verification.json", report)
    return report


def abort_drain(approval):
    record = active()
    if approval != record["plan_sha256"]:
        raise ValueError("Abort must identify the original maintenance plan")
    if record["stage"] == "failed":
        return record
    if record["stage"] not in {"prepared", "draining"}:
        raise ValueError("Only maintenance before any Terraform apply can be aborted")
    aws = Aws(PROFILES["platform"])
    aws.preflight("platform")
    instance = aws.call("ec2", "describe-instances", "--instance-ids", record["instance_id"])[
        "Reservations"
    ][0]["Instances"][0]
    if (
        instance["State"]["Name"] != "running"
        or instance["InstanceType"] != record["previous_type"]
    ):
        raise ValueError("Abort cannot undo an ambiguous AWS transition")
    if record["stage"] == "draining":
        set_cordon(record, False, observed_type=record["previous_type"])
        if record.get("frontend"):
            from platform_frontend import restore

            restore(record)
    record.update(
        stage="failed",
        aborted_at=utc_now(),
        reason="Interrupted drain aborted before any AWS modification",
    )
    save(record)
    return record


def recover(record):
    from image_credentials import healthy_targets
    from smoke import wait_until

    aws = Aws(PROFILES["platform"])

    def running_ready():
        instance = aws.call("ec2", "describe-instances", "--instance-ids", record["instance_id"])[
            "Reservations"
        ][0]["Instances"][0]
        value = node(record["node"])
        if value["metadata"]["uid"] != record["node_uids"][record["node"]]:
            raise ValueError("The resized host must retain its Kubernetes identity")
        return (
            instance
            if instance["State"]["Name"] == "running"
            and instance["InstanceType"] == record["desired_type"]
            and ready(value)
            and sufficient_capacity(value)
            else None
        )

    instance = wait_until(
        running_ready, "resized worker and original Node readiness", timeout=300, interval=5
    )
    record.update(stage="recovering", running_at=utc_now(), cpu_options=instance["CpuOptions"])
    save(record)
    set_cordon(record, False)

    def restored():
        try:
            observed = healthy_targets()
            return (
                observed
                if {k: v["uid"] for k, v in observed.items()} == record["node_uids"]
                else None
            )
        except ValueError:
            return None

    wait_until(
        restored,
        "all three platform workers and database replicas recovered",
        timeout=300,
        interval=5,
    )
    allocation()
    from platform_frontend import restore

    restore(record)
    record.update(
        stage="completed",
        completed_at=utc_now(),
        allocatable=node(record["node"])["status"]["allocatable"],
    )
    save(record)
    return record


def apply(approval):
    from cluster import kubectl
    from database_failover import backup_reference
    from image_credentials import healthy_targets
    from manage import apply_plan, plan_paths, verify_receipt
    from smoke import wait_until

    record = active()
    if approval != record["plan_sha256"]:
        raise ValueError("Approval must identify the exact prepared CPU resize")
    if record["stage"] == "completed":
        return record
    if record["stage"] in {"applying", "recovering"}:
        # Reconcile the original AWS transition; never automatically replay Terraform.
        return recover(record)
    if record["stage"] != "prepared":
        raise ValueError("Interrupted drain requires inspection; no automatic second drain/apply")
    receipt = json.loads(plan_paths("platform")[1].read_text())
    verify_receipt(receipt, approval, source_digest(), "platform")
    if {k: v["uid"] for k, v in healthy_targets().items()} != record["node_uids"]:
        raise ValueError("Platform identities changed after review")
    backup_reference(record["backup"]["operation"], record["database_uid"])
    no_active_jobs()
    allocation()
    record.update(stage="draining", drain_started_at=utc_now())
    save(record)
    set_cordon(record, True)
    try:
        kubectl(
            "drain",
            record["node"],
            "--ignore-daemonsets",
            "--delete-emptydir-data",
            "--timeout=180s",
        )
        wait_until(lambda: not workload_pods(record["node"]), "selected worker drain", timeout=60)

        def surviving():
            try:
                assert_survivors(record)
                return True
            except ValueError:
                return False

        wait_until(
            surviving,
            "two database replicas and all management roles recovered off-host",
            timeout=180,
            interval=3,
        )
        from platform_frontend import detach

        detach(record)
        record.update(stage="applying", drained_at=utc_now())
        save(record)
        apply_plan("platform", approval)
    except Exception:
        # No unsafe eviction, forced promotion or hidden downgrade. Preserve the
        # journal/cordon after an ambiguous AWS change for explicit reconciliation.
        record["interrupted_at"] = utc_now()
        save(record)
        raise
    return recover(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "apply", "observe", "abort-drain", "verify"])
    parser.add_argument("--node", choices=sorted(NODES))
    parser.add_argument("--backup-operation")
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            result = prepare(args.node, args.backup_operation)
        elif args.command == "apply":
            result = apply(args.approve_plan)
        elif args.command == "abort-drain":
            result = abort_drain(args.approve_plan)
        elif args.command == "verify":
            result = verify_capacity()
        else:
            result = active()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
