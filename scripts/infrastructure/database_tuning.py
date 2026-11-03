"""Review/apply only the pooled-client smart-shutdown fix through the owning Helm release."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json

import yaml
from common import ROOT, private_dir, source_digest, state_home, tool, utc_now, write_private
from database import NAME, NAMESPACE, verify
from database_recovery import resource
from image_credentials import healthy_targets


def folder():
    return private_dir(state_home() / "cluster/database/tuning")


def helm(*args):
    from cluster import FOLDER, checked, kubeconfigs, tunnel

    kubeconfigs()
    with tunnel():
        return checked(
            [tool("helm"), *args, "--kubeconfig", str(FOLDER / "operator.conf")], timeout=660
        )


def documents(text):
    result = {}
    for item in yaml.safe_load_all(text):
        if item:
            key = (
                item["apiVersion"],
                item["kind"],
                item["metadata"].get("namespace", NAMESPACE),
                item["metadata"]["name"],
            )
            if key in result:
                raise ValueError("Duplicate resource in database tuning manifest")
            result[key] = item
    return result


def assert_only_shutdown_change(before, after):
    old, new = documents(before), documents(after)
    if set(old) != set(new):
        raise ValueError("Database tuning cannot add/remove resources")
    changes = []
    for key in old:
        expected = copy.deepcopy(old[key])
        if key == ("postgresql.cnpg.io/v1", "Cluster", NAMESPACE, NAME):
            expected["spec"]["smartShutdownTimeout"] = 5
            changes.append({"resource": NAME, "field": "spec.smartShutdownTimeout", "value": 5})
        if new[key] != expected:
            raise ValueError("Database tuning contains changes beyond the smart shutdown window")
    if len(changes) != 1:
        raise ValueError("Database tuning must affect exactly the owning Cluster")
    return changes


def render(values):
    from cluster import checked

    return checked(
        [
            tool("helm"),
            "template",
            NAME,
            str(ROOT / "charts/klip-database"),
            "-n",
            NAMESPACE,
            "--values",
            str(values),
        ]
    )


def prepare():
    before = verify()
    targets = healthy_targets()
    status = json.loads(helm("status", NAME, "-n", NAMESPACE, "-o", "json"))
    values = yaml.safe_load(helm("get", "values", NAME, "-n", NAMESPACE, "--all", "-o", "yaml"))
    previous = helm("get", "manifest", NAME, "-n", NAMESPACE)
    values["smartShutdownTimeout"] = 5
    write_private(folder() / "values.yaml", yaml.safe_dump(values))
    desired = render(folder() / "values.yaml")
    changes = assert_only_shutdown_change(previous, desired)
    again = json.loads(helm("status", NAME, "-n", NAMESPACE, "-o", "json"))
    if again["version"] != status["version"]:
        raise ValueError("Database Helm release changed during preparation")
    current = resource("cluster", NAME)
    if current["metadata"]["uid"] != before["cluster_uid"]:
        raise ValueError("Database identity changed during preparation")
    plan = {
        "prepared_at": utc_now(),
        "source_sha256": source_digest(),
        "cluster_uid": before["cluster_uid"],
        "release_revision": status["version"],
        "current_smart_shutdown_seconds": current["spec"]["smartShutdownTimeout"],
        "spec_sha256": hashlib.sha256(
            json.dumps(current["spec"], sort_keys=True).encode()
        ).hexdigest(),
        "values_sha256": hashlib.sha256((folder() / "values.yaml").read_bytes()).hexdigest(),
        "previous_manifest_sha256": hashlib.sha256(previous.encode()).hexdigest(),
        "manifest_sha256": hashlib.sha256(desired.encode()).hexdigest(),
        "instances": before["instances"],
        "targets": targets,
        "changes": changes,
    }
    plan["approval_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    write_private(folder() / "plan.json", plan)
    write_private(folder() / "rendered.yaml", desired)
    return plan


def apply(approval):
    plan = json.loads((folder() / "plan.json").read_text())
    if (
        plan.get("applied_at")
        or approval != plan["approval_sha256"]
        or source_digest() != plan["source_sha256"]
    ):
        raise ValueError("Database tuning requires the unchanged, unapplied reviewed plan")
    before = verify()
    current = resource("cluster", NAME)
    if (
        before["cluster_uid"] != plan["cluster_uid"]
        or before["instances"] != plan["instances"]
        or healthy_targets() != plan["targets"]
        or current["spec"]["smartShutdownTimeout"] != plan["current_smart_shutdown_seconds"]
        or hashlib.sha256(json.dumps(current["spec"], sort_keys=True).encode()).hexdigest()
        != plan["spec_sha256"]
    ):
        raise ValueError("Database tuning target changed after review")
    values = folder() / "values.yaml"
    previous = helm("get", "manifest", NAME, "-n", NAMESPACE)
    if (
        hashlib.sha256(values.read_bytes()).hexdigest() != plan["values_sha256"]
        or hashlib.sha256(previous.encode()).hexdigest() != plan["previous_manifest_sha256"]
        or hashlib.sha256(render(values).encode()).hexdigest() != plan["manifest_sha256"]
    ):
        raise ValueError("Database tuning release or frozen inputs changed")
    plan["started_at"] = utc_now()
    write_private(folder() / "plan.json", plan)
    output = helm(
        "upgrade",
        NAME,
        str(ROOT / "charts/klip-database"),
        "-n",
        NAMESPACE,
        "--values",
        str(values),
        "--atomic",
        "--wait",
        "--timeout",
        "10m",
    )
    write_private(folder() / "helm.log", output)
    after = verify()
    current = resource("cluster", NAME)
    if (
        after["cluster_uid"] != before["cluster_uid"]
        or after["instances"] != before["instances"]
        or current["spec"]["smartShutdownTimeout"] != 5
    ):
        raise ValueError("Database tuning changed instance identities or did not apply")
    plan.update(applied_at=utc_now(), verification=after)
    write_private(folder() / "plan.json", plan)
    return plan


if __name__ == "__main__":
    import fcntl

    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "apply"])
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(
            json.dumps(
                prepare() if args.command == "prepare" else apply(args.approve_plan), indent=2
            )
        )
