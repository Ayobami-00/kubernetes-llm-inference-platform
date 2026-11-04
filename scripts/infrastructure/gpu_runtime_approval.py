"""Stage an exact, cluster-bound GPU review without renting or changing a host."""

import argparse
import json
from pathlib import Path

from common import source_digest, state_home, utc_now, write_private
from gpu_runtime_security import REVIEW, assess, configuration, digest, sha
from host_executor_upgrade import identities, ready_pods
from platform_management.integrations.job_runner import frozen_file


def folder():
    return state_home() / "cluster/gpu-runtime-approval"


def observe_cluster():
    from cluster import Host, kubectl, load_hosts
    from host_executor_release import folder as executor_folder

    cp = Host(load_hosts()["cp-01"])
    identity, pods = identities(cp)
    active = json.loads((executor_folder() / "active.json").read_text())
    ready_pods(pods, active["image"])
    policy = json.loads(kubectl("get", "clusterpolicy", "cluster-policy", "-o", "json"))
    deployment = json.loads(
        kubectl("-n", "gpu-system", "get", "deployment", "gpu-operator", "-o", "json")
    )
    if deployment.get("status", {}).get("availableReplicas") != 1:
        raise ValueError("GPU Operator controller must be available before staging review")
    observed = {
        "identities": identity,
        "image": active["image"],
        "operator": {
            "deployment_uid": deployment["metadata"]["uid"],
            "policy_uid": policy["metadata"]["uid"],
            "policy_spec_sha256": digest(policy["spec"]),
        },
    }
    return cp, sorted(p["metadata"]["name"] for p in pods)[0], observed


def prepare(images, host):
    evidence = assess(images, host)
    _, _, observed = observe_cluster()
    review = json.loads(REVIEW.read_text())
    from gpu_operator import settings

    pins = configuration()
    receipt = {
        "schema_version": "klip-gpu-runtime-approval.v1",
        "profile": "single-a10",
        "kube_system_uid": observed["identities"]["/namespace/kube-system"],
        "reviewed_at": review["reviewed_at"],
        "expires_at": review["expires_at"],
        "review_sha256": sha(REVIEW.read_bytes()),
        "operator_lock_sha256": pins["gpu-operator.lock.json"],
        "host_lock_sha256": pins["gpu-host.lock.json"],
        "base_artifacts_sha256": pins["artifacts.lock.json"],
        "images": settings()["images"],
        "operator": observed["operator"],
        "assessment_sha256": sha(json.dumps(evidence, sort_keys=True).encode()),
    }
    plan = {
        "schema_version": "klip-gpu-runtime-stage.v1",
        "prepared_at": utc_now(),
        "source_sha256": source_digest([REVIEW, Path(__file__)]),
        "observed": observed,
        "approval": receipt,
        "assessment": evidence,
        "resource_envelope": {"gpu_instances": 0, "host_mutations": 0},
    }
    identity = digest(plan)
    path = folder() / identity
    frozen_file(path / "plan.json", json.dumps(plan, sort_keys=True).encode())
    return {"plan_sha256": identity, "status": "prepared", **plan["resource_envelope"]}


STAGE = r"""
import fcntl,json,sys
from contextlib import ExitStack
from pathlib import Path
from platform_management.host_executor.gpu_approval import approval, REVIEW
from platform_management.integrations.job_runner import frozen_file
import hashlib
document=json.load(sys.stdin)
root=Path('/executor/state')
with ExitStack() as stack:
    for name in ('dispatch.lock','cluster/operator.lock'):
        lock=stack.enter_context((root/name).open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if hashlib.sha256(REVIEW.read_bytes()).hexdigest()!=document['approval']['review_sha256']:
        raise ValueError('Executor image does not contain the reviewed runtime definition')
    peers=json.loads((root/'cluster/peers.json').read_text())
    if set(peers['peers'])!={'cp-01','cp-02','cp-03','platform-01','platform-02','platform-03'}:
        raise ValueError('Initial GPU approval requires the six CPU peers only')
    for path in (root/'executions').glob('host-*/task.json'):
        observation=path.parent/'observation.json'
        value=json.loads(observation.read_text()) if observation.is_file() else {}
        if value.get('status') not in ('completed','failed') or value.get('cleanup_pending'):
            raise ValueError('Unfinished host mutation prevents approval staging')
    frozen_file(root/'cluster/gpu-runtime-assessment.json',
                json.dumps(document['assessment'],sort_keys=True).encode())
    frozen_file(root/'cluster/gpu-runtime-approval.json',
                json.dumps(document['approval'],sort_keys=True).encode())
    result=approval(root,document['approval']['kube_system_uid'])
    print(json.dumps({'status':'staged','review_sha256':result['review_sha256'],
                      'assessment_sha256':result['assessment_sha256']}))
"""


def apply(identity):
    from datetime import UTC, datetime

    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Exact approval plan required")
    path = folder() / identity
    plan = json.loads((path / "plan.json").read_text())
    if digest(plan) != identity or source_digest([REVIEW, Path(__file__)]) != plan["source_sha256"]:
        raise ValueError("Approval plan or versioned source changed")
    if datetime.now(UTC) >= datetime.fromisoformat(plan["approval"]["expires_at"]):
        raise ValueError("GPU runtime review expired")
    cp, pod, observed = observe_cluster()
    if observed != plan["observed"]:
        raise ValueError("Cluster, executor image or GPU Operator identity changed")
    execution = path / "execution.json"
    if execution.exists():
        raise ValueError("Retain original attempt; inspect its receipt before any recovery")
    write_private(execution, {"status": "staging", "started_at": utc_now(), "pod": pod})
    try:
        output = cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "platform-ops",
                "exec",
                "-i",
                pod,
                "--",
                "python",
                "-c",
                STAGE,
            ],
            data=json.dumps(plan),
            timeout=45,
        )
        result = json.loads(output)
        if result != {
            "status": "staged",
            "review_sha256": plan["approval"]["review_sha256"],
            "assessment_sha256": plan["approval"]["assessment_sha256"],
        }:
            raise ValueError("Executor did not verify the staged assessment")
        result["verified_at"] = utc_now()
    except Exception as error:
        write_private(
            execution, {"status": "reconciliation_required", "error_type": type(error).__name__}
        )
        raise
    write_private(execution, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "apply"])
    parser.add_argument("--image-directory", type=Path)
    parser.add_argument("--host-directory", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    if args.command == "prepare":
        if not args.image_directory or not args.host_directory:
            parser.error("prepare requires both report directories")
        result = prepare(args.image_directory, args.host_directory)
    else:
        result = apply(args.approve_plan or "")
    print(json.dumps(result, indent=2))
