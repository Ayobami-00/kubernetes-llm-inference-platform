"""Durable CPU-only fixture staging on the real platform, followed by management runs."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import yaml
from common import ROOT, private_dir, source_digest, state_home, tool, utc_now, write_private
from image_release import assess, guard_workloads
from management_release import folder as management_folder

SCENARIOS = (
    "writer-assist-baseline",
    "tutor-chat-baseline",
    "doc-flow-baseline",
    "research-agent-baseline",
    "code-agent-baseline",
    "shared-contention",
    "platform-mixed",
)
ENDPOINT = "http://inference-stub.klip.svc:8001/v1/chat/completions"


def location(operation):
    if not re.fullmatch(r"[a-f0-9]{12}", operation):
        raise ValueError("Use a stable twelve-character hexadecimal acceptance identity")
    return private_dir(state_home() / "cluster/management/workload-acceptance" / operation)


def artifact_root(operation):
    location(operation)
    return "/artifacts/acceptance/" + operation


def images():
    plan = json.loads((management_folder() / "plan.json").read_text())
    source = plan["image_assessment"]["source_commit"]
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            source,
            "--",
            "apps/management",
            "packages",
            "workload-runner",
            "dev/inference-stub",
            "uv.lock",
            "configs",
            "datasets/manifests",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    # Tooling-only root configuration changes do not change the pinned runtime.
    return assess(Path(plan["receipts"]), source)


def render_stub(image):
    from cluster import checked

    return checked(
        [
            tool("helm"),
            "template",
            "klip-workload-stub",
            str(ROOT / "charts/klip-workload-stub"),
            "--namespace",
            "klip",
            "--values",
            "-",
        ],
        data=yaml.safe_dump({"image": image}),
    )


def preparation_job(operation, image):
    root = artifact_root(operation)
    program = (
        "from pathlib import Path\nimport subprocess\n"
        f"root=Path({root!r})\n"
        "root.mkdir(parents=True,exist_ok=False)\n"
        "def run(*args): subprocess.run(['workload-runner','--repo','/app',*args],check=True)\n"
        "run('prepare','--source','synthetic','--limit','100','--output',str(root/'prepared'))\n"
        f"for scenario in {SCENARIOS!r}:\n"
        " run('plan','--scenario',scenario,'--prepared',str(root/'prepared'),'--smoke',"
        f"'--endpoint',{ENDPOINT!r},'--output',str(root/'plans'/scenario))\n"
    )
    labels = {"app.kubernetes.io/managed-by": "klip", "klip.dev/acceptance": operation}
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "klip-prepare-" + operation, "namespace": "klip", "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 300,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "klip-runner",
                    "automountServiceAccountToken": False,
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
                            "name": "prepare",
                            "image": image,
                            "command": ["python", "-c", program],
                            "env": [
                                {"name": k, "value": v}
                                for k, v in {
                                    "LANG": "C.UTF-8",
                                    "LC_ALL": "C.UTF-8",
                                    "LANGUAGE": "C",
                                    "PYTHONDONTWRITEBYTECODE": "1",
                                    "HOME": "/tmp",
                                }.items()
                            ],
                            "resources": {
                                "requests": {"cpu": "1", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "artifacts", "mountPath": "/artifacts"},
                                {"name": "temporary", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "artifacts",
                            "persistentVolumeClaim": {"claimName": "klip-artifacts"},
                        },
                        {"name": "temporary", "emptyDir": {"sizeLimit": "512Mi"}},
                    ],
                },
            },
        },
    }
    job["metadata"]["annotations"] = {
        "klip.dev/preparation-sha256": hashlib.sha256(
            json.dumps(job, sort_keys=True).encode()
        ).hexdigest()
    }
    return job


def validate_preparation(job, expected):
    """Accept API defaults while binding every authored input and privilege boundary."""

    def contains(actual, wanted):
        if isinstance(wanted, dict):
            return isinstance(actual, dict) and all(
                k in actual and contains(actual[k], v) for k, v in wanted.items()
            )
        if isinstance(wanted, list):
            return (
                isinstance(actual, list)
                and len(actual) == len(wanted)
                and all(contains(a, w) for a, w in zip(actual, wanted, strict=True))
            )
        return actual == wanted

    if not contains(job, expected) or job["spec"]["template"]["spec"].get("initContainers"):
        raise ValueError("Preparation Job differs from its frozen inputs")
    guard_workloads([job])


def read_artifact(operation, relative):
    from cluster import Host, kubectl, load_hosts

    if relative not in {"plans/" + s + "/management-bundle.json" for s in SCENARIOS}:
        raise ValueError("Only generated synthetic bundles may be read through this proof")
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
        raise ValueError("No ready management API Pod can read the shared artifact mount")
    pod = min(ready, key=lambda p: p["metadata"]["name"])
    program = (
        "from pathlib import Path;import sys; p=Path(sys.argv[1]); "
        "assert p.is_file() and not p.is_symlink() and p.stat().st_size<4194304; "
        "print(p.read_text())"
    )
    return json.loads(
        Host(load_hosts()["cp-01"]).run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "klip",
                "exec",
                pod["metadata"]["name"],
                "-c",
                "api",
                "--",
                "python",
                "-c",
                program,
                artifact_root(operation) + "/" + relative,
            ],
            secret=True,
        )
    )


def stage(operation):
    from cluster import FOLDER, Host, checked, kubeconfigs, kubectl, load_hosts, tunnel
    from image_credentials import healthy_targets
    from telemetry_budget import verify

    root = location(operation)
    path = root / "stage.json"
    if path.exists():
        record = json.loads(path.read_text())
        if record["status"] == "prepared":
            return record
        if record["source_sha256"] != source_digest():
            raise ValueError("Preserve the interrupted staging attempt; source changed")
    else:
        assessment = images()
        manifest = render_stub(assessment["images"]["stub"]["image"])
        documents = [d for d in yaml.safe_load_all(manifest) if d]
        guard_workloads(documents)
        job = preparation_job(operation, assessment["images"]["runner"]["image"])
        guard_workloads([job])
        record = {
            "operation": operation,
            "status": "planned",
            "created_at": utc_now(),
            "source_sha256": source_digest(),
            "targets": healthy_targets(),
            "request_budget": verify(documents, "workload-stub"),
            "images": assessment,
            "preparation": job,
            "fixture_data": True,
        }
        write_private(path, record)
        write_private(root / "stub-rendered.yaml", manifest)
        write_private(
            root / "stub-values.yaml",
            yaml.safe_dump({"image": assessment["images"]["stub"]["image"]}),
        )
    cp = Host(load_hosts()["cp-01"])
    if record["status"] == "planned":
        # Persist the side-effect boundary before Helm; never blindly replay an ambiguous rollout.
        record.update(status="deploying", helm_started_at=utc_now())
        write_private(path, record)
        kubeconfigs()
        with tunnel():
            output = checked(
                [
                    tool("helm"),
                    "upgrade",
                    "--install",
                    "klip-workload-stub",
                    str(ROOT / "charts/klip-workload-stub"),
                    "--namespace",
                    "klip",
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "--values",
                    str(root / "stub-values.yaml"),
                    "--atomic",
                    "--wait",
                    "--timeout",
                    "3m",
                ],
                timeout=240,
            )
        write_private(root / "helm.log", output)
        record.update(status="stub_ready", stub_ready_at=utc_now())
        write_private(path, record)
    if record["status"] == "deploying":
        raise ValueError("Observe the original Helm attempt before continuing; no automatic replay")
    name = record["preparation"]["metadata"]["name"]
    existing = kubectl("-n", "klip", "get", "job", name, "--ignore-not-found", "-o", "json")
    if not existing.strip():
        if record.get("job_uid") or record["status"] == "preparing":
            raise ValueError("Preparation Job was lost; never restart an abandoned attempt")
        template = record["preparation"]["spec"]["template"]
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name + "-admission-canary",
                "namespace": "klip",
                "labels": template["metadata"]["labels"],
            },
            "spec": template["spec"],
        }
        cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "create",
                "--dry-run=server",
                "-f",
                "-",
                "-o",
                "name",
            ],
            data=json.dumps(pod),
        )
        record.update(status="preparing", job_submitted_at=utc_now())
        write_private(path, record)
        existing = cp.run(
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
            data=json.dumps(record["preparation"]),
        )
    job = json.loads(existing)
    validate_preparation(job, record["preparation"])
    uid = job["metadata"]["uid"]
    if record.get("job_uid", uid) != uid:
        raise ValueError("Preparation Job UID changed")
    record.update(status="preparing", job_uid=uid)
    write_private(path, record)
    deadline = time.monotonic() + 330
    while True:
        job = json.loads(kubectl("-n", "klip", "get", "job", name, "-o", "json"))
        if job["metadata"]["uid"] != uid:
            raise ValueError("Preparation Job replaced")
        validate_preparation(job, record["preparation"])
        conditions = {
            c["type"] for c in job.get("status", {}).get("conditions", []) if c["status"] == "True"
        }
        if "Failed" in conditions:
            record.update(status="failed", failed_at=utc_now())
            write_private(path, record)
            raise ValueError("Preparation failed; retain its logs and select a new attempt")
        if "Complete" in conditions:
            break
        if time.monotonic() > deadline:
            raise ValueError("Preparation observation expired; original Job remains authoritative")
        time.sleep(2)
    record["bundles"] = {}
    for scenario in SCENARIOS:
        bundle = read_artifact(operation, "plans/" + scenario + "/management-bundle.json")
        write_private(root / (scenario + ".json"), bundle)
        record["bundles"][scenario] = hashlib.sha256(
            json.dumps(bundle, sort_keys=True).encode()
        ).hexdigest()
    record.update(status="prepared", prepared_at=utc_now())
    write_private(path, record)
    return record


def run(operation, scenario):
    from workload_runner.management import submit_bundle

    root = location(operation)
    record = json.loads((root / "stage.json").read_text())
    if record["status"] != "prepared" or scenario not in record["bundles"]:
        raise ValueError("Prepare the exact seven-scenario bundle first")
    bundle_path = root / (scenario + ".json")
    if (
        hashlib.sha256(
            json.dumps(json.loads(bundle_path.read_text()), sort_keys=True).encode()
        ).hexdigest()
        != record["bundles"][scenario]
    ):
        raise ValueError("Staged immutable bundle changed")
    result = submit_bundle(
        bundle_path,
        "https://management.klip-lab.click",
        ROOT / "dev/.secrets/admin_password",
        {},
        "aws-cpu-" + operation + "-" + scenario,
        root / "results" / scenario,
    )
    return {
        "scenario": scenario,
        "run_id": result.id,
        "status": result.status,
        "validity": result.result.validity if result.result else None,
        "fixture": result.fixture,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stage", "run"])
    parser.add_argument("--operation", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS)
    args = parser.parse_args()
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "run" and not args.scenario:
            parser.error("run requires --scenario")
        print(
            json.dumps(
                stage(args.operation)
                if args.command == "stage"
                else run(args.operation, args.scenario),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
