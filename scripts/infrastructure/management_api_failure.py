"""One UID-bound API Pod loss; the caller independently probes authenticated HTTPS."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import time

from common import private_dir, source_digest, utc_now, write_private
from management_release import folder
from smoke import wait_until

SELECTOR = "app.kubernetes.io/name=klip,app.kubernetes.io/component=api"


def select_target(deployment, pods, replicasets, image, targets):
    status = deployment.get("status", {})
    if (
        deployment["metadata"]["name"] != "klip-api"
        or deployment["spec"]["replicas"] != 2
        or status.get("observedGeneration") != deployment["metadata"]["generation"]
        or any(
            status.get(k) != 2 for k in ("readyReplicas", "updatedReplicas", "availableReplicas")
        )
        or len(pods) != 2
        or len({p["spec"]["nodeName"] for p in pods}) != 2
    ):
        raise ValueError("API restart requires two available replicas on distinct hosts")
    owners = {
        r["metadata"]["uid"]
        for r in replicasets
        if any(
            o.get("controller")
            and o["kind"] == "Deployment"
            and o["uid"] == deployment["metadata"]["uid"]
            for o in r["metadata"].get("ownerReferences", [])
        )
    }
    for pod in pods:
        if (
            pod["metadata"].get("namespace") != "klip"
            or not re.fullmatch(r"klip-api-[a-z0-9]+-[a-z0-9]+", pod["metadata"]["name"])
            or pod["metadata"].get("deletionTimestamp")
            or pod["spec"]["nodeName"] not in targets
            or len(pod["spec"]["containers"]) != 1
            or pod["spec"]["containers"][0]["image"] != image
            or not any(
                o.get("controller") and o["kind"] == "ReplicaSet" and o["uid"] in owners
                for o in pod["metadata"].get("ownerReferences", [])
            )
            or not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod.get("status", {}).get("conditions", [])
            )
        ):
            raise ValueError("API restart target ownership, placement or readiness differs")
    pod = min(pods, key=lambda p: p["metadata"]["name"])
    return {
        "name": pod["metadata"]["name"],
        "uid": pod["metadata"]["uid"],
        "node": pod["spec"]["nodeName"],
    }


def observe():
    from cluster import kubectl

    return (
        json.loads(kubectl("-n", "klip", "get", "deployment", "klip-api", "-o", "json")),
        json.loads(kubectl("-n", "klip", "get", "pods", "-l", SELECTOR, "-o", "json"))["items"],
        json.loads(kubectl("-n", "klip", "get", "replicasets", "-l", SELECTOR, "-o", "json"))[
            "items"
        ],
    )


def run(operation):
    from cluster import delete_owned_resource, kubectl
    from database import verify

    if not re.fullmatch(r"[a-f0-9]{12}", operation):
        raise ValueError("Use a stable 12-character lowercase hexadecimal operation")
    root = private_dir(folder() / "failures" / operation)
    path = root / "api.json"
    with (root / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            record = json.loads(path.read_text())
            if record["stage"] == "replaced":
                return record
        else:
            verify()  # Never introduce the first fault into an already-degraded DB.
            plan = json.loads((folder() / "plan.json").read_text())
            active = json.loads((folder() / "active.json").read_text())
            if plan["approval_sha256"] != active["approval_sha256"]:
                raise ValueError("API restart requires the current deployed release plan")
            image = plan["image_assessment"]["images"]["backend"]["image"]
            allocation = observe()
            selected = select_target(*allocation, image, plan["targets"])
            namespace = json.loads(kubectl("get", "namespace", "klip", "-o", "json"))
            credentials = json.loads((folder() / "credentials.json").read_text())
            if namespace["metadata"]["uid"] != credentials["namespace_uid"]:
                raise ValueError("API restart namespace identity changed")
            record = {
                "operation": operation,
                "stage": "prepared",
                "target": selected,
                "deployment_uid": allocation[0]["metadata"]["uid"],
                "namespace_uid": namespace["metadata"]["uid"],
                "image": image,
                "release_approval": active["approval_sha256"],
                "source_sha256": source_digest(),
                "targets": plan["targets"],
                "created_at": utc_now(),
            }
            write_private(path, record)
        if record["stage"] == "prepared":
            deployment, pods, replicasets = observe()
            select_target(deployment, pods, replicasets, record["image"], record["targets"])
            namespace = json.loads(kubectl("get", "namespace", "klip", "-o", "json"))
            if (
                deployment["metadata"]["uid"] != record["deployment_uid"]
                or namespace["metadata"]["uid"] != record["namespace_uid"]
                or not any(p["metadata"]["uid"] == record["target"]["uid"] for p in pods)
            ):
                raise ValueError("Prepared API restart identity changed before submission")
            record.update(stage="submitted", intervention_at=utc_now())
            write_private(path, record)
            target = record["target"]
            delete_owned_resource("/api/v1/namespaces/klip/pods/" + target["name"], target["uid"])
        # An interrupted submission is observed, never resubmitted against a new Pod.
        started = time.monotonic()

        def restored():
            deployment, pods, replicasets = observe()
            if deployment["metadata"]["uid"] != record["deployment_uid"]:
                raise ValueError("API Deployment changed during failure acceptance")
            if any(p["metadata"]["uid"] == record["target"]["uid"] for p in pods):
                return None
            try:
                select_target(deployment, pods, replicasets, record["image"], record["targets"])
            except ValueError:
                return None
            return [
                {
                    "name": p["metadata"]["name"],
                    "uid": p["metadata"]["uid"],
                    "node": p["spec"]["nodeName"],
                }
                for p in pods
            ]

        record["replacement_pods"] = wait_until(
            restored, "API replacement and spreading", timeout=120, interval=1
        )
        record.update(
            stage="replaced",
            replacement_observed_at=utc_now(),
            observation_wait_seconds=round(time.monotonic() - started, 3),
        )
        write_private(path, record)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    print(json.dumps(run(parser.parse_args().operation)))


if __name__ == "__main__":
    main()
