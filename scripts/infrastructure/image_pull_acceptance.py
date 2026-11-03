"""Real private pulls and identity denials; no application listener or credentials in Pods."""

from __future__ import annotations

import argparse
import json
import re
import uuid

import yaml
from common import CONFIG, state_home, utc_now, write_private
from image_credentials import healthy_targets
from smoke import wait_until

ROLE = f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-images-pull"


def job_document(operation: str, case: str, node: str, image: str, namespace: str) -> dict:
    if (
        not re.fullmatch(r"[0-9a-f]{12}", operation)
        or case not in {"positive", "missing-annotation", "wrong-namespace"}
        or node not in {"platform-01", "platform-02", "platform-03"}
        or not re.fullmatch(
            r"127811062476\.dkr\.ecr\.eu-west-2\.amazonaws\.com/klip/management-backend@sha256:[0-9a-f]{64}",
            image,
        )
        or namespace != ("klip-image-denied-" + operation if case == "wrong-namespace" else "klip")
    ):
        raise ValueError("Unreviewed image-pull verification target")
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"klip-image-{operation}-{node[-2:]}-{case}",
            "namespace": namespace,
            "annotations": {"klip.dev/verification-id": operation},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 180,
            "template": {
                "spec": {
                    "nodeName": node,
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "serviceAccountName": "default"
                    if case == "missing-annotation"
                    else "klip-image-pull-probe",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "check",
                            "image": image,
                            "imagePullPolicy": "Always",
                            "command": [
                                "/usr/local/bin/python",
                                "-c",
                                "print('private-image-pull-ok')",
                            ],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "32Mi"},
                                "limits": {"cpu": "250m", "memory": "128Mi"},
                            },
                            "securityContext": {
                                "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                }
            },
        },
    }


def denied(waiting: dict) -> bool:
    return waiting.get("reason") in {"ErrImagePull", "ImagePullBackOff"} and any(
        marker in waiting.get("message", "").lower()
        for marker in (
            "unauthorized",
            "forbidden",
            "no basic auth credentials",
            "missing required",
            "403",
            "401",
            "not authorized",
        )
    )


def execution_complete(case: str, job_status: dict, pod_phase: str | None) -> bool:
    if job_status.get("failed") or pod_phase == "Failed":
        raise ValueError("Image-pull verification workload failed")
    if case != "positive" and pod_phase == "Succeeded":
        raise ValueError("An unauthorized identity executed the private image")
    # Job status can lag a completed Pod. Wait for controller reconciliation.
    return case == "positive" and job_status.get("succeeded") == 1 and pod_phase == "Succeeded"


def verify(image: str) -> dict:
    from cluster import FOLDER, Host, delete_owned_resource, kubectl, load_hosts

    healthy_targets()
    operation = uuid.uuid4().hex[:12]
    job_document(operation, "positive", "platform-01", image, "klip")
    path = FOLDER / "image-pull-checks" / (operation + ".json")
    report = {
        "schema_version": "klip-image-pull-verification.v1",
        "operation": operation,
        "started_at": utc_now(),
        "image": image,
        "status": "running",
        "checks": [],
        "jobs": [],
        "created": [],
        "cleanup_pending": True,
    }
    namespace_uid = None
    namespace = "klip-image-denied-" + operation
    cp = Host(load_hosts()["cp-01"])

    def save():
        write_private(path, report)

    def create(doc):
        doc["metadata"].setdefault("annotations", {})["klip.dev/verification-id"] = operation
        report["create_pending"] = {"kind": doc["kind"], "metadata": doc["metadata"]}
        save()
        try:
            created = json.loads(
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
                    data=yaml.safe_dump(doc),
                )
            )
        except RuntimeError:
            args = [
                "get",
                doc["kind"].lower(),
                doc["metadata"]["name"],
                "--ignore-not-found",
                "-o",
                "json",
            ]
            if doc["kind"] != "Namespace":
                args += ["-n", doc["metadata"]["namespace"]]
            observed = kubectl(*args).strip()
            if not observed:
                raise
            created = json.loads(observed)
            if (
                created["metadata"].get("annotations", {}).get("klip.dev/verification-id")
                != operation
            ):
                raise ValueError("Ambiguous creation found a foreign verification object") from None
        report["created"].append(
            {
                "kind": doc["kind"],
                "name": created["metadata"]["name"],
                "namespace": created["metadata"].get("namespace"),
                "uid": created["metadata"]["uid"],
            }
        )
        report.pop("create_pending", None)
        save()
        return created

    save()
    try:
        cases = [("positive", n, "klip") for n in healthy_targets()]
        cases += [
            ("missing-annotation", "platform-01", "klip"),
            ("wrong-namespace", "platform-01", namespace),
        ]
        for case, node, ns in cases:
            report["current_check"] = {"case": case, "node": node, "namespace": ns}
            save()
            if case == "wrong-namespace":
                created = create(
                    {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "metadata": {
                            "name": ns,
                            "annotations": {"klip.dev/verification-id": operation},
                            "labels": {
                                "pod-security.kubernetes.io/enforce": "restricted",
                                "pod-security.kubernetes.io/enforce-version": "v1.36",
                            },
                        },
                    }
                )
                namespace_uid = created["metadata"]["uid"]
                report["namespace_uid"] = namespace_uid
                save()
                create(
                    {
                        "apiVersion": "v1",
                        "kind": "ServiceAccount",
                        "metadata": {
                            "namespace": ns,
                            "name": "klip-image-pull-probe",
                            "annotations": {"eks.amazonaws.com/ecr-role-arn": ROLE},
                        },
                        "automountServiceAccountToken": False,
                    }
                )
            created = create(job_document(operation, case, node, image, ns))
            name, uid = created["metadata"]["name"], created["metadata"]["uid"]
            report["jobs"].append({"namespace": ns, "name": name, "uid": uid})
            save()

            def outcome(ns=ns, name=name, uid=uid, node=node, case=case):
                job = json.loads(kubectl("-n", ns, "get", "job", name, "-o", "json"))
                if job["metadata"]["uid"] != uid:
                    raise ValueError("Verification Job identity changed")
                pods = json.loads(
                    kubectl("-n", ns, "get", "pods", "-l", "job-name=" + name, "-o", "json")
                )["items"]
                if len(pods) > 1:
                    raise ValueError("A verification Job must never restart")
                if not pods:
                    return None
                pod = pods[0]
                if pod["spec"]["nodeName"] != node or pod["spec"].get("volumes"):
                    raise ValueError("Verification placement/token exposure changed")
                states = pod.get("status", {}).get("containerStatuses", [])
                waiting = states[0].get("state", {}).get("waiting", {}) if states else {}
                if execution_complete(
                    case, job.get("status", {}), pod.get("status", {}).get("phase")
                ):
                    if (
                        kubectl("-n", ns, "logs", pod["metadata"]["name"]).strip()
                        != "private-image-pull-ok"
                    ):
                        raise ValueError("Private image did not run the expected command")
                    return {"case": case, "node": node, "outcome": "pulled-and-executed"}
                if case != "positive" and denied(waiting):
                    return {
                        "case": case,
                        "node": node,
                        "outcome": "denied",
                        "reason": waiting["reason"],
                    }
                return None

            report["checks"].append(
                wait_until(outcome, case + " private pull on " + node, timeout=150, interval=2)
            )
            save()
        report.update(status="passed", verified_at=utc_now())
    except Exception:
        report.update(status="failed", failed_at=utc_now())
        raise
    finally:
        save()
        report["jobs"] = [
            {k: v for k, v in r.items() if k != "kind"}
            for r in report["created"]
            if r["kind"] == "Job"
        ]
        namespace_uid = next(
            (r["uid"] for r in report["created"] if r["kind"] == "Namespace"), None
        )
        for job in report["jobs"]:
            delete_owned_resource(
                f"/apis/batch/v1/namespaces/{job['namespace']}/jobs/{job['name']}",
                job["uid"],
            )
        if namespace_uid:
            delete_owned_resource("/api/v1/namespaces/" + namespace, namespace_uid)
            wait_until(
                lambda: (
                    not kubectl(
                        "get", "namespace", namespace, "--ignore-not-found", "-o", "name"
                    ).strip()
                ),
                "verification namespace cleanup",
                timeout=90,
            )
        for job in report["jobs"]:
            wait_until(
                lambda job=job: (
                    not kubectl(
                        "-n",
                        job["namespace"],
                        "get",
                        "job",
                        job["name"],
                        "--ignore-not-found",
                        "-o",
                        "name",
                    ).strip()
                ),
                "verification Job cleanup",
                timeout=90,
            )
            wait_until(
                lambda job=job: (
                    not json.loads(
                        kubectl(
                            "-n",
                            job["namespace"],
                            "get",
                            "pods",
                            "-l",
                            "job-name=" + job["name"],
                            "-o",
                            "json",
                        )
                    )["items"]
                ),
                "verification Pod cleanup",
                timeout=90,
            )
        report["cleanup_pending"] = "create_pending" in report
        if report["status"] == "passed":
            report.pop("current_check", None)
        save()
        write_private(state_home() / "reports/image-pulls.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    print(json.dumps(verify(parser.parse_args().image), indent=2))
