"""Bind management's existing lifecycle chart to verified provider/executor custody."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from common import private_dir, state_home, utc_now
from gpu_runtime_approval import observe_cluster
from gpu_runtime_security import REVIEW, digest
from management_release import folder
from platform_management.integrations.lambda_policy import LambdaLifecyclePolicy
from platform_management.providers.lambda_prerequisites import PrerequisiteSpec

SECRET = "klip-bootstrap-seed"


def policy_from_receipt(value):
    spec = PrerequisiteSpec.model_validate(value["spec"])
    if (
        value.get("schema_version") != "klip-lambda-prerequisite-result.v1"
        or value.get("state") != "ready"
        or value.get("setup_id") != spec.identity
        or value.get("spec_sha256") != digest(spec.model_dump(mode="json"))
        or value.get("gpu_capacity_provisioned") is not False
        or set(value.get("resources", {})) != {"ssh", "firewall"}
        or any(r.get("state") != "ready" for r in value["resources"].values())
    ):
        raise ValueError("Verified provider prerequisite receipt required")
    return LambdaLifecyclePolicy(
        kube_system_uid=spec.kube_system_uid,
        image_id="6c389693-2b2d-47a3-93da-052c2331bd98",
        ssh_key_name=spec.names["ssh"],
        ssh_public_key_sha256=hashlib.sha256(spec.public_key.encode()).hexdigest(),
        firewall_ruleset_ids=(value["resources"]["firewall"]["id"],),
    )


def executor_metadata(cp, pod):
    return json.loads(
        cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "platform-ops",
                "exec",
                pod,
                "--",
                "python",
                "-c",
                "import json; from pathlib import Path; "
                "from platform_management.host_executor.api import metadata; "
                "print(json.dumps(metadata(Path('/executor/state'))))",
            ]
        )
    )


def stage(prerequisites):
    from cluster import kubectl
    from platform_management.integrations.job_runner import frozen_file

    value = json.loads(prerequisites.read_text())
    policy = policy_from_receipt(value)
    cp, pod, observed = observe_cluster()
    metadata = executor_metadata(cp, pod)
    policy.check_metadata(metadata)
    review_sha = hashlib.sha256(REVIEW.read_bytes()).hexdigest()
    if (
        metadata.get("gpu_inspection_configured") is not True
        or metadata.get("gpu_runtime_review_sha256") != review_sha
    ):
        raise ValueError("Live executor has no matching runtime approval; lifecycle stays disabled")
    credentials = private_dir(state_home() / "credentials")
    seed = credentials / "lambda-bootstrap-seed"
    if not seed.exists():
        descriptor = os.open(seed, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
    if seed.is_symlink() or seed.stat().st_size != 32 or seed.stat().st_mode & 0o077:
        raise ValueError("Bootstrap seed must be a private, exact 32-byte regular file")
    body = seed.read_bytes()
    fingerprint = hashlib.sha256(body).hexdigest()
    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "immutable": True,
        "metadata": {
            "name": SECRET,
            "namespace": "klip",
            "annotations": {
                "klip.dev/project": "kubernetes-llm-inference-platform",
                "klip.dev/seed-sha256": fingerprint,
                "klip.dev/kube-system-uid": policy.kube_system_uid,
            },
        },
        "data": {"seed": base64.b64encode(body).decode()},
    }
    intent = {
        "schema_version": "klip-lambda-lifecycle-stage.v1",
        "policy": policy.model_dump(mode="json"),
        "seed_sha256": fingerprint,
        "provider_receipt_sha256": hashlib.sha256(prerequisites.read_bytes()).hexdigest(),
        "runtime_review_sha256": review_sha,
        "observed": observed,
    }
    root = folder() / "lifecycle"
    frozen_file(root / "intent.json", json.dumps(intent, sort_keys=True).encode())
    current = kubectl(
        "-n",
        "klip",
        "get",
        "secret",
        SECRET,
        "--ignore-not-found",
        "-o",
        "jsonpath={.metadata.uid}",
    ).strip()
    if current:
        # Do not trust just caller-editable annotations when reconciling a lost create.
        existing = json.loads(
            cp.run(
                [
                    "sudo",
                    "kubectl",
                    "--kubeconfig=/etc/kubernetes/admin.conf",
                    "-n",
                    "klip",
                    "get",
                    "secret",
                    SECRET,
                    "-o",
                    "json",
                ],
                secret=True,
            )
        )
        if (
            existing.get("immutable") is not True
            or existing.get("data") != document["data"]
            or any(
                existing["metadata"].get("annotations", {}).get(k) != v
                for k, v in document["metadata"]["annotations"].items()
            )
        ):
            raise ValueError("Bootstrap Secret differs; never replace existing credentials")
        uid = existing["metadata"]["uid"]
    else:
        created = json.loads(
            cp.run(
                [
                    "sudo",
                    "kubectl",
                    "--kubeconfig=/etc/kubernetes/admin.conf",
                    "-n",
                    "klip",
                    "create",
                    "-f",
                    "-",
                    "-o",
                    "json",
                ],
                data=json.dumps(document),
                secret=True,
            )
        )
        uid = created["metadata"]["uid"]
    receipt = {
        **intent,
        "secret_uid": uid,
        "staged_at": utc_now(),
        "chart_values": {
            "enabled": True,
            "clientTLSSecret": "klip-host-client-tls",
            "bootstrapSeedSecret": SECRET,
            "bootstrapSeedKey": "seed",
            "policy": policy.model_dump(mode="json"),
        },
    }
    if (root / "staged.json").exists():
        previous = json.loads((root / "staged.json").read_text())
        if any(previous.get(k) != v for k, v in intent.items()) or previous["secret_uid"] != uid:
            raise ValueError("Original lifecycle staging receipt changed")
        receipt = previous
    # Staging credentials does not change the running application or rent a GPU.
    frozen_file(root / "staged.json", json.dumps(receipt, sort_keys=True).encode())
    return {
        "status": "staged",
        "secret_uid": uid,
        "policy": receipt["policy"],
        "gpu_capacity_provisioned": False,
    }


def configuration():
    """Recheck custody at both release planning and deployment; no secret reads."""
    from cluster import kubectl

    path = folder() / "lifecycle/staged.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    policy = LambdaLifecyclePolicy.model_validate(value["policy"])
    uid = kubectl("-n", "klip", "get", "secret", SECRET, "-o", "jsonpath={.metadata.uid}").strip()
    cp, pod, observed = observe_cluster()
    if uid != value["secret_uid"] or observed != value["observed"]:
        raise ValueError("Lifecycle seed or executor/operator custody changed")
    metadata = executor_metadata(cp, pod)
    policy.check_metadata(metadata)
    if (
        metadata.get("gpu_inspection_configured") is not True
        or metadata.get("gpu_runtime_review_sha256") != value["runtime_review_sha256"]
    ):
        raise ValueError("Lifecycle runtime approval is missing or expired")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-receipt", type=Path, required=True)
    print(json.dumps(stage(parser.parse_args().provider_receipt), indent=2))
