"""Replace only the isolated executor image, retaining custody and release history.

The first upgrade is deliberately CPU-only: no allocated GPU or unfinished host
operation may exist. A native, non-retrying guard Job holds the executor's actual
dispatch and peer-writer locks throughout preflight and the rolling release.
"""

import argparse
import copy
import fcntl
import json
import re
import time
from pathlib import Path

import yaml
from common import ROOT, source_digest, state_home, tool, utc_now, write_private
from host_executor_release import (
    NAMESPACE,
    PREFLIGHT_PROGRAM,
    bootstrap_job,
    checked_image_source,
    custody,
    folder,
    rendered,
    sha,
)
from host_executor_security import assess
from platform_management.integrations.job_runner import frozen_file

GUARD_PROGRAM = r"""
import fcntl,json,os,time
from contextlib import ExitStack
from pathlib import Path
root=Path('/executor/state')
with ExitStack() as stack:
    for name in ('dispatch.lock','cluster/operator.lock'):
        lock=stack.enter_context((root/name).open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    peers=json.loads((root/'cluster/peers.json').read_text())
    if set(peers['peers'])!={'cp-01','cp-02','cp-03','platform-01','platform-02','platform-03'}:
        raise ValueError('CPU-only upgrade requires no allocated GPU peers')
    for path in (root/'executions').glob('host-*/task.json'):
        observed=path.parent/'observation.json'
        value=json.loads(observed.read_text()) if observed.is_file() else {}
        if value.get('status') not in ('completed','failed') or value.get('cleanup_pending'):
            raise ValueError('Unfinished host task prevents an executor upgrade')
    from platform_management.integrations.job_runner import frozen_file
    directory=root/'upgrades'/os.environ['KLIP_UPGRADE_ID']
    frozen_file(directory/'guard.json',json.dumps({'pod_uid':os.environ['POD_UID'],
        'dispatch_fenced':True,'peer_writer_fenced':True,'registry_revision':peers['revision']},
        sort_keys=True).encode())
    while not (directory/'release').is_file(): time.sleep(1)
"""


def mapped(text):
    documents = [d for d in yaml.safe_load_all(text) if d]
    result = {(d["kind"], d["metadata"]["name"]): d for d in documents}
    if len(result) != len(documents):
        raise ValueError("Duplicate executor resource identities")
    return result


def validate_delta(previous, candidate, old_image, new_image):
    old, new = mapped(previous), mapped(candidate)
    if old_image == new_image or set(old) != set(new):
        raise ValueError("Upgrade must change only the existing executor image")
    expected = copy.deepcopy(old)
    deployment = expected[("Deployment", "host-executor")]
    containers = deployment["spec"]["template"]["spec"]["containers"]
    if len(containers) != 1 or containers[0]["image"] != old_image:
        raise ValueError("Previous executor image differs from retained release")
    container = containers[0]
    bindings = [e for e in container["env"] if e["name"] == "HOST_EXECUTOR_IMAGE"]
    if len(bindings) != 1 or bindings[0].get("value") != old_image:
        raise ValueError("Previous native Job image binding differs")
    container["image"], bindings[0]["value"] = new_image, new_image
    if new != expected:
        raise ValueError(
            "Image upgrade cannot change storage, credentials, privileges or networking"
        )


def ready_pods(pods, image):
    if len(pods) != 2 or len({p["spec"].get("nodeName") for p in pods}) != 2:
        raise ValueError("Executor requires two ready replicas on distinct CPU hosts")
    for pod in pods:
        containers = pod["spec"]["containers"]
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if (
            len(containers) != 1
            or containers[0]["image"] != image
            or len(statuses) != 1
            or not statuses[0].get("ready")
            or not statuses[0].get("imageID", "").endswith(image.split("@")[1])
            or not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod.get("status", {}).get("conditions", [])
            )
            or not pod["spec"]
            .get("nodeSelector", {})
            .get("klip.dev.node-restriction.kubernetes.io/platform")
        ):
            raise ValueError("Executor Pod readiness, placement or runtime digest differs")
    return [{"uid": p["metadata"]["uid"], "node": p["spec"]["nodeName"]} for p in pods]


def identities(cp):
    from cluster import kubectl

    value = {}
    for namespace, kind, name in (
        ("", "namespace", "kube-system"),
        ("", "namespace", NAMESPACE),
        (NAMESPACE, "deployment", "host-executor"),
        (NAMESPACE, "pvc", "klip-host-state"),
        (NAMESPACE, "secret", "klip-host-credentials"),
        (NAMESPACE, "secret", "klip-host-server-tls"),
        ("klip", "secret", "klip-host-client-tls"),
    ):
        scope = ["-n", namespace] if namespace else []
        # Metadata-only output; never retrieve retained Secret payloads.
        value["/".join((namespace, kind, name))] = kubectl(
            *scope, "get", kind, name, "-o", "jsonpath={.metadata.uid}"
        ).strip()
    pods = json.loads(
        kubectl(
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=klip-host-executor,app.kubernetes.io/component=api",
            "-o",
            "json",
        )
    )["items"]
    return value, pods


def prepare(receipt, directory):
    from cluster import Host, load_hosts

    review = assess(receipt, directory)
    checked_image_source(review)
    active_body = (folder() / "active.json").read_bytes()
    active = json.loads(active_body)
    old_values = (folder() / "values.yaml").read_text()
    values = yaml.safe_load(old_values)
    if values["image"] != active["image"]:
        raise ValueError("Retained values differ from the active executor image")
    old = (folder() / "rendered.yaml").read_text()
    values["image"] = review["image"]
    # Render before any external change; retain the complete exact candidate.
    temporary = folder() / "upgrade-candidate-values.yaml"
    write_private(temporary, yaml.safe_dump(values))
    candidate, _ = rendered(temporary)
    validate_delta(old, candidate, active["image"], review["image"])
    cp = Host(load_hosts()["cp-01"])
    identity, pods = identities(cp)
    ready_pods(pods, active["image"])
    record = {
        "schema_version": "klip-host-image-upgrade.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "active_sha256": sha(active_body),
        "identities": identity,
        "custody_fingerprints": custody(),
        "image_assessment": review,
        "receipt_path": str(receipt.resolve()),
        "security_directory": str(directory.resolve()),
        "previous_values_sha256": sha(old_values.encode()),
        "previous_rendered_sha256": sha(old.encode()),
        "values_sha256": sha(temporary.read_bytes()),
        "rendered_sha256": sha(candidate.encode()),
    }
    approval = sha(json.dumps(record, sort_keys=True).encode())
    record["approval_sha256"] = approval
    root = folder() / "upgrades" / approval
    for name, content in {
        "previous-active.json": active_body,
        "previous-values.yaml": old_values.encode(),
        "previous-rendered.yaml": old.encode(),
        "values.yaml": temporary.read_bytes(),
        "rendered.yaml": candidate.encode(),
        "plan.json": json.dumps(record, sort_keys=True).encode(),
    }.items():
        frozen_file(root / name, content)
    return record


def checked_plan(approval):
    if not isinstance(approval, str) or not re.fullmatch(r"[a-f0-9]{64}", approval):
        raise ValueError("Exact saved executor upgrade approval required")
    root = folder() / "upgrades" / approval
    plan = json.loads((root / "plan.json").read_text())
    if (
        approval
        != sha(
            json.dumps(
                {k: v for k, v in plan.items() if k != "approval_sha256"}, sort_keys=True
            ).encode()
        )
        or plan["approval_sha256"] != approval
        or plan["source_sha256"] != source_digest()
        or plan["active_sha256"] != sha((folder() / "active.json").read_bytes())
        or plan["previous_values_sha256"] != sha((folder() / "values.yaml").read_bytes())
        or plan["previous_rendered_sha256"] != sha((folder() / "rendered.yaml").read_bytes())
        or plan["values_sha256"] != sha((root / "values.yaml").read_bytes())
        or plan["active_sha256"] != sha((root / "previous-active.json").read_bytes())
        or plan["previous_values_sha256"] != sha((root / "previous-values.yaml").read_bytes())
        or plan["previous_rendered_sha256"] != sha((root / "previous-rendered.yaml").read_bytes())
        or plan["rendered_sha256"] != sha((root / "rendered.yaml").read_bytes())
        or plan["rendered_sha256"] != sha(rendered(root / "values.yaml")[0].encode())
        or plan["image_assessment"]
        != assess(Path(plan["receipt_path"]), Path(plan["security_directory"]))
        or plan["custody_fingerprints"] != custody()
    ):
        raise ValueError("Executor upgrade source, custody, scan or retained release changed")
    checked_image_source(plan["image_assessment"])
    validate_delta(
        (root / "previous-rendered.yaml").read_text(),
        (root / "rendered.yaml").read_text(),
        json.loads((root / "previous-active.json").read_text())["image"],
        plan["image_assessment"]["image"],
    )
    return root, plan


def apply(approval):
    from cluster import FOLDER, Host, checked, kubeconfigs, kubectl, load_hosts, tunnel
    from workload_acceptance import validate_preparation

    root, plan = checked_plan(approval)
    result_path = root / "result.json"
    if result_path.exists():
        raise ValueError("Existing upgrade must be observed/reconciled; never restart it blindly")
    cp = Host(load_hosts()["cp-01"])
    identity, _ = identities(cp)
    if identity != plan["identities"]:
        raise ValueError("Executor cluster, deployment, storage or credential identities changed")
    job = bootstrap_job(plan, list(mapped((root / "rendered.yaml").read_text()).values()))
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["command"] = ["python", "-c", GUARD_PROGRAM]
    container["env"] += [
        {"name": "KLIP_UPGRADE_ID", "value": approval},
        {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
    ]
    frozen_file(root / "guard-job.json", json.dumps(job, sort_keys=True).encode())
    result = {"approval_sha256": approval, "status": "running", "started_at": utc_now()}
    write_private(result_path, result)  # Intent precedes the native create.
    command = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "-n", NAMESPACE]
    pod = None
    try:
        observed = json.loads(
            cp.run([*command, "create", "-f", "-", "-o", "json"], data=json.dumps(job))
        )
        validate_preparation(observed, job)
        result.update(job_name=job["metadata"]["name"], job_uid=observed["metadata"]["uid"])
        write_private(result_path, result)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            pods = json.loads(
                kubectl(
                    "-n",
                    NAMESPACE,
                    "get",
                    "pods",
                    "-l",
                    "job-name=" + job["metadata"]["name"],
                    "-o",
                    "json",
                )
            )["items"]
            if len(pods) == 1 and pods[0].get("status", {}).get("phase") == "Running":
                pod = pods[0]
                break
            if any(p.get("status", {}).get("phase") in {"Failed", "Succeeded"} for p in pods):
                raise ValueError("Upgrade guard terminated before custody preflight")
            time.sleep(1)
        if not pod or not any(
            o["uid"] == result["job_uid"] for o in pod["metadata"].get("ownerReferences", [])
        ):
            raise ValueError("Original upgrade guard did not start with its bound Job UID")
        execute = [*command, "exec", pod["metadata"]["name"], "--", "python", "-c"]
        guard_path = "/executor/state/upgrades/" + approval + "/guard.json"
        guard = json.loads(
            cp.run(
                [
                    *execute,
                    "from pathlib import Path; import time\n"
                    + "p=Path("
                    + repr(guard_path)
                    + "); deadline=time.monotonic()+30\n"
                    + "while not p.is_file() and time.monotonic()<deadline: time.sleep(.1)\n"
                    + "print(p.read_text())",
                ],
                timeout=40,
            )
        )
        if (
            guard.get("pod_uid") != pod["metadata"]["uid"]
            or not guard.get("dispatch_fenced")
            or not guard.get("peer_writer_fenced")
        ):
            raise ValueError("Upgrade does not hold authoritative dispatch and peer-writer custody")
        proof = json.loads(cp.run([*execute, PREFLIGHT_PROGRAM], secret=True))
        if proof["kube_system_uid"] != identity["/namespace/kube-system"]:
            raise ValueError("Upgrade preflight reached another cluster")
        result.update(preflight=proof, guard=guard, pod_uid=pod["metadata"]["uid"])
        write_private(result_path, result)
        kubeconfigs()
        with (state_home() / "cluster/operator.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            checked_plan(approval)
            with tunnel():
                output = checked(
                    [
                        tool("helm"),
                        "upgrade",
                        "host-executor",
                        str(ROOT / "charts/klip-host-executor"),
                        "-n",
                        NAMESPACE,
                        "--kubeconfig",
                        str(FOLDER / "operator.conf"),
                        "--values",
                        str(root / "values.yaml"),
                        "--atomic",
                        "--wait",
                        "--timeout",
                        "5m",
                        "--history-max",
                        "10",
                    ],
                    timeout=360,
                )
            write_private(root / "helm.log", output)
            after, pods = identities(cp)
            if after != identity or custody() != plan["custody_fingerprints"]:
                raise ValueError("Executor custody changed during its rolling upgrade")
            result["api_pods"] = ready_pods(pods, plan["image_assessment"]["image"])
            native = json.loads(
                kubectl("-n", NAMESPACE, "get", "job", result["job_name"], "-o", "json")
            )
            if (
                native["metadata"]["uid"] != result["job_uid"]
                or native.get("status", {}).get("failed")
                or native.get("status", {}).get("active") != 1
            ):
                raise ValueError("Upgrade guard was replaced or failed during deployment")
            # No handoff, peer registry, SSH key or TLS Secret is rewritten.
            active = json.loads((root / "previous-active.json").read_text())
            result.update(
                status="deployed", deployed_at=utc_now(), image=plan["image_assessment"]["image"]
            )
            active["image"] = result["image"]
            active.setdefault("upgrades", []).append(copy.deepcopy(result))
            write_private(folder() / "values.yaml", (root / "values.yaml").read_text())
            write_private(folder() / "rendered.yaml", (root / "rendered.yaml").read_text())
            write_private(folder() / "active.json", active)
    except Exception as error:
        result.update(status="reconciliation_required", error_type=type(error).__name__)
        raise
    finally:
        write_private(result_path, result)
        if pod:
            # Releasing the guard does not retry a failed upgrade. If transport
            # is lost, the native 900-second deadline releases both locks.
            try:
                cp.run(
                    [
                        *command,
                        "exec",
                        pod["metadata"]["name"],
                        "--",
                        "python",
                        "-c",
                        "from pathlib import Path; p=Path('/executor/state/upgrades/"
                        + approval
                        + "/release'); p.parent.mkdir(parents=True,exist_ok=True); p.touch()",
                    ]
                )
            except Exception:
                write_private(
                    root / "guard-release-pending.json",
                    {"job_uid": result.get("job_uid"), "native_deadline_seconds": 900},
                )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply"])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--security-directory", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    value = (
        prepare(args.receipt, args.security_directory)
        if args.action == "prepare"
        else apply(args.approve_plan)
    )
    print(json.dumps(value, indent=2))
