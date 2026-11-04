"""Run free Lambda prerequisite setup as a bounded, receipt-owned CPU Job."""

import argparse
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

from common import ROOT, source_digest, state_home, utc_now, write_private
from image_release import assess, guard_workloads
from management_release import AUTH, CA, DATABASE
from platform_management.integrations.job_runner import frozen_file
from platform_management.providers.lambda_prerequisites import PrerequisiteSpec


def sha(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def folder():
    return state_home() / "cluster/lambda-provider-setup"


def manifest(spec, image, attempt):
    if not re.fullmatch(r"[a-f0-9]{20}", attempt):
        raise ValueError("Expected a fixed provider setup attempt identity")
    name = "lambda-prereq-" + attempt
    result = f"provider-setup/{spec.identity}/{attempt}/result.json"
    labels = {"app.kubernetes.io/name": "klip", "app.kubernetes.io/component": "worker"}
    spec_body = encoded(spec.model_dump(mode="json"))
    config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "immutable": True,
        "metadata": {
            "name": "lambda-prereq-" + spec.identity.removeprefix("lambda-setup-"),
            "namespace": "klip",
            "annotations": {
                "klip.dev/project": "kubernetes-llm-inference-platform",
                "klip.dev/input-sha256": sha(spec_body),
            },
        },
        "data": {"provider-setup.json": spec_body.decode()},
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": "klip",
            "labels": labels,
            "annotations": {
                "klip.dev/provider-setup": spec.identity,
                "klip.dev/input-sha256": sha(spec_body),
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 180,
            "ttlSecondsAfterFinished": 86400,
            "podReplacementPolicy": "Failed",
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "serviceAccountName": "klip-migrate",
                    "nodeSelector": {"klip.dev.node-restriction.kubernetes.io/platform": "true"},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "provider-setup",
                            "image": image,
                            "command": [
                                "python",
                                "-m",
                                "platform_management.jobs.provider_prerequisites",
                                "--spec",
                                "/run/config/provider-setup.json",
                                "--output",
                                "/artifacts/" + result,
                            ],
                            "env": [
                                *[
                                    {"name": k, "value": v}
                                    for k, v in {
                                        "LANG": "C.UTF-8",
                                        "LC_ALL": "C.UTF-8",
                                        "LANGUAGE": "C",
                                        "MANAGEMENT_DATABASE_REQUIRE_TLS": "true",
                                        "MANAGEMENT_DATABASE_CA_FILE": (
                                            "/run/secrets/database/ca.crt"
                                        ),
                                        "MANAGEMENT_LAMBDA_API_KEY_FILE": (
                                            "/run/secrets/provider/lambda-api-key"
                                        ),
                                        "MANAGEMENT_HOST_EXECUTOR_TLS_HOME": (
                                            "/run/secrets/host-client"
                                        ),
                                        "MANAGEMENT_REAL_LIFECYCLE_ENABLED": "false",
                                    }.items()
                                ],
                                {
                                    "name": "MANAGEMENT_DATABASE_URL",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": DATABASE, "key": "database-url"}
                                    },
                                },
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "volumeMounts": [
                                {"name": n, "mountPath": p, "readOnly": True}
                                for n, p in (
                                    ("config", "/run/config"),
                                    ("provider", "/run/secrets/provider"),
                                    ("database-ca", "/run/secrets/database"),
                                    ("host-client", "/run/secrets/host-client"),
                                )
                            ]
                            + [
                                {"name": "artifacts", "mountPath": "/artifacts"},
                                {"name": "temporary", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": config["metadata"]["name"]}},
                        {
                            "name": "provider",
                            "secret": {
                                "secretName": AUTH,
                                "defaultMode": 0o440,
                                "items": [{"key": "lambda-api-key", "path": "lambda-api-key"}],
                            },
                        },
                        {
                            "name": "database-ca",
                            "secret": {
                                "secretName": CA,
                                "defaultMode": 0o440,
                                "items": [{"key": "ca.crt", "path": "ca.crt"}],
                            },
                        },
                        {
                            "name": "host-client",
                            "secret": {"secretName": "klip-host-client-tls", "defaultMode": 0o440},
                        },
                        {
                            "name": "artifacts",
                            "persistentVolumeClaim": {"claimName": "klip-artifacts"},
                        },
                        {"name": "temporary", "emptyDir": {"sizeLimit": "64Mi"}},
                    ],
                },
            },
        },
    }
    guard_workloads([job])
    return config, job, result


def context():
    from cluster import Host, kubectl, load_hosts

    cp = Host(load_hosts()["cp-01"])
    pods = json.loads(
        kubectl(
            "-n",
            "platform-ops",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=klip-host-executor,app.kubernetes.io/component=api",
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
    if len(ready) != 2:
        raise ValueError("Provider setup requires both verified executor API replicas")
    program = """import json
from pathlib import Path
p=Path('/executor/state/cluster')
value=json.loads((p/'executor-inputs.json').read_text())
print(json.dumps({'kube_system_uid':value['kube_system_uid'],
    'public_key':(p/'lambda-ssh-key.pub').read_text().strip(),
    'cpu_public_ips':{k:v['public_ip']+'/32' for k,v in value['cpu_peers'].items()}}))
"""
    values = json.loads(
        cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "platform-ops",
                "exec",
                ready[0]["metadata"]["name"],
                "--",
                "python",
                "-c",
                program,
            ]
        )
    )
    identities = {}
    for namespace, kind, name in (
        ("", "namespace", "kube-system"),
        ("", "namespace", "klip"),
        ("platform-ops", "deployment", "host-executor"),
        ("klip", "secret", AUTH),
        ("klip", "secret", DATABASE),
        ("klip", "secret", CA),
        ("klip", "secret", "klip-host-client-tls"),
        ("klip", "pvc", "klip-artifacts"),
    ):
        scope = ["-n", namespace] if namespace else []
        identities["/".join((namespace, kind, name))] = kubectl(
            *scope, "get", kind, name, "-o", "jsonpath={.metadata.uid}"
        ).strip()
    if identities["/namespace/kube-system"] != values["kube_system_uid"]:
        raise ValueError("Executor handoff belongs to another cluster")
    return cp, values, identities


def image_assessment(receipts):
    source = json.loads(next(receipts.rglob("backend.json")).read_text())["source_commit"]
    paths = [
        "apps/management/backend",
        "packages",
        "pyproject.toml",
        "uv.lock",
        ".dockerignore",
        "scripts/management/stage_workspace.py",
    ]
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", source, "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "diff", "--exit-code", source, "--", *paths],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    if subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *paths], cwd=ROOT
    ):
        raise ValueError("Untracked application inputs differ from the provider Job image")
    return assess(receipts.resolve(), source)


def prepare(receipts):
    assessed = image_assessment(receipts)
    _, public, identities = context()
    key = (ROOT / "dev/.secrets/lambda_api_key").read_text().strip()
    if not key or any(c in key for c in "\r\n"):
        raise ValueError("Provider credential reference is absent or invalid")
    spec = PrerequisiteSpec(**public, provider_credential_sha256=sha(key.encode()))
    attempt = uuid.uuid4().hex[:20]
    config, job, output = manifest(spec, assessed["images"]["backend"]["image"], attempt)
    value = {
        "schema_version": "klip-lambda-provider-job.v1",
        "created_at": utc_now(),
        "attempt": attempt,
        "setup_id": spec.identity,
        "source_sha256": source_digest(),
        "receipts": str(receipts.resolve()),
        "image_assessment": assessed,
        "identities": identities,
        "spec_sha256": sha(encoded(spec.model_dump(mode="json"))),
        "config_sha256": sha(encoded(config)),
        "job_sha256": sha(encoded(job)),
        "output": output,
        "resource_envelope": {
            "new_gpu_instances": 0,
            "additional_gpu_cost": 0,
            "cpu_job_deadline_seconds": 180,
        },
    }
    approval = sha(encoded(value))
    value["approval_sha256"] = approval
    root = folder() / approval
    for name, document in {
        "plan.json": value,
        "spec.json": spec.model_dump(mode="json"),
        "configmap.json": config,
        "job.json": job,
    }.items():
        frozen_file(root / name, encoded(document))
    return value


def checked_plan(approval):
    if not isinstance(approval, str) or not re.fullmatch(r"[a-f0-9]{64}", approval):
        raise ValueError("Exact saved provider setup plan identity required")
    root = folder() / approval
    plan = json.loads((root / "plan.json").read_text())
    if (
        approval != sha(encoded({k: v for k, v in plan.items() if k != "approval_sha256"}))
        or plan["approval_sha256"] != approval
        or plan["source_sha256"] != source_digest()
        or plan["image_assessment"] != image_assessment(Path(plan["receipts"]))
        or plan["spec_sha256"] != sha((root / "spec.json").read_bytes())
        or plan["config_sha256"] != sha((root / "configmap.json").read_bytes())
        or plan["job_sha256"] != sha((root / "job.json").read_bytes())
    ):
        raise ValueError("Provider setup source, image, saved inputs or plan changed")
    return root, plan


def apply(approval):
    from cluster import kubectl
    from workload_acceptance import validate_preparation

    root, plan = checked_plan(approval)
    cp, _, ids = context()
    if ids != plan["identities"]:
        raise ValueError("Provider setup cluster or mounted credential identities changed")
    result_path = root / "execution.json"
    if result_path.exists():
        raise ValueError(
            "Original provider setup attempt already exists; observe it, never recreate it"
        )
    config = json.loads((root / "configmap.json").read_text())
    job = json.loads((root / "job.json").read_text())
    value = {"status": "staging", "started_at": utc_now(), "approval_sha256": approval}
    write_private(result_path, value)
    k = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "-n", "klip"]
    current = kubectl(
        "-n",
        "klip",
        "get",
        "configmap",
        config["metadata"]["name"],
        "--ignore-not-found",
        "-o",
        "json",
    )
    if current.strip():
        observed = json.loads(current)
        if (
            observed.get("data") != config["data"]
            or observed.get("immutable") is not True
            or observed["metadata"].get("annotations", {}).get("klip.dev/project")
            != "kubernetes-llm-inference-platform"
            or observed["metadata"].get("annotations", {}).get("klip.dev/input-sha256")
            != plan["spec_sha256"]
        ):
            raise ValueError("Refusing to adopt a changed or unowned provider setup ConfigMap")
    else:
        observed = json.loads(
            cp.run([*k, "create", "-f", "-", "-o", "json"], data=json.dumps(config))
        )
    value.update(
        status="dispatching",
        configmap_uid=observed["metadata"]["uid"],
        job_name=job["metadata"]["name"],
    )
    write_private(result_path, value)
    native = json.loads(cp.run([*k, "create", "-f", "-", "-o", "json"], data=json.dumps(job)))
    validate_preparation(native, job)
    value.update(status="running", job_uid=native["metadata"]["uid"])
    write_private(result_path, value)
    return value


def observe(approval):
    from cluster import kubectl
    from workload_acceptance import validate_preparation

    root, plan = checked_plan(approval)
    value = json.loads((root / "execution.json").read_text())
    if value["status"] in {"ready", "failed"}:
        return value
    expected = json.loads((root / "job.json").read_text())
    text = kubectl(
        "-n", "klip", "get", "job", expected["metadata"]["name"], "--ignore-not-found", "-o", "json"
    )
    if not text.strip():
        raise ValueError(
            "Original provider setup Job is missing; reconcile dispatch, never recreate it"
        )
    native = json.loads(text)
    validate_preparation(native, expected)
    if value.get("job_uid") not in {None, native["metadata"]["uid"]}:
        raise ValueError("Provider setup native Job identity changed")
    value["job_uid"] = native["metadata"]["uid"]
    conditions = {
        c["type"] for c in native.get("status", {}).get("conditions", []) if c["status"] == "True"
    }
    if "Failed" in conditions:
        value.update(status="failed", observed_at=utc_now(), reconciliation_required=True)
    elif "Complete" in conditions:
        cp, _, ids = context()
        if ids != plan["identities"]:
            raise ValueError("Provider setup custody changed during execution")
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
            if any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p.get("status", {}).get("conditions", [])
            )
        ]
        if not ready:
            raise ValueError("No management API replica can verify the retained artifact")
        artifact = "/artifacts/" + plan["output"]
        program = (
            "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
            "assert not p.is_symlink() and p.stat().st_size<=65536; print(p.read_text())"
        )
        output = cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "klip",
                "exec",
                ready[0]["metadata"]["name"],
                "--",
                "python",
                "-c",
                program,
                artifact,
            ]
        )
        result = json.loads(output)
        if (
            result.get("schema_version") != "klip-lambda-prerequisite-result.v1"
            or result.get("state") != "ready"
            or result.get("setup_id") != plan["setup_id"]
            or result.get("gpu_capacity_provisioned") is not False
            or result.get("spec_sha256") != plan["spec_sha256"]
            or result.get("spec") != json.loads((root / "spec.json").read_text())
            or set(result.get("resources", {})) != {"ssh", "firewall"}
            or any(
                r.get("state") != "ready" or not r.get("id") for r in result["resources"].values()
            )
        ):
            raise ValueError("Provider setup artifact does not confirm its frozen intent")
        frozen_file(root / "provider-result.json", encoded(result))
        value.update(
            status="ready",
            verified_at=utc_now(),
            artifact_sha256=sha(encoded(result)),
            provider_account_id=result["account_id"],
            resources=result["resources"],
            gpu_capacity_provisioned=False,
        )
    write_private(root / "execution.json", value)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply", "observe"])
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    value = (
        prepare(args.receipts)
        if args.action == "prepare"
        else {"apply": apply, "observe": observe}[args.action](args.approve_plan)
    )
    print(json.dumps(value, indent=2))
