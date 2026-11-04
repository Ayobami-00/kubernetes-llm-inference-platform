"""Stage fixed host custody and deploy its source/scan-bound isolated service."""

import argparse
import base64
import copy
import fcntl
import hashlib
import hmac
import json
import re
import shlex
import subprocess
from pathlib import Path

import yaml
from common import (
    CONFIG,
    ROOT,
    private_dir,
    source_digest,
    state_home,
    tf,
    tool,
    utc_now,
    write_private,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from host_executor_security import assess
from host_executor_tls import material
from image_release import guard_workloads

NAMESPACE = "platform-ops"
CPU_NAMES = {"cp-01", "cp-02", "cp-03", "platform-01", "platform-02", "platform-03"}
CONTAINER_ROOT = Path("/executor/state")
FILES = {
    "reports/foundation.json",
    "cluster/peers.json",
    "cluster/inventory.yaml",
    "cluster/ca.crt",
    "cluster/cpu-known-hosts",
    "cluster/lambda-ssh-key.pub",
    "cluster/executor-inputs.json",
    "cluster/peer-owner.json",
}


def folder():
    return private_dir(state_home() / "cluster/host-executor")


def sha(content):
    return hashlib.sha256(content).hexdigest()


def custody():
    material(folder() / "tls")
    values = {
        role: sha((folder() / "tls" / role / "tls.crt").read_bytes())
        for role in ("server", "client")
    }
    values["ca"] = sha((folder() / "tls/ca.crt").read_bytes())
    for name, filename in (
        ("foundation_ssh", "foundation_ed25519"),
        ("lambda_ssh", "lambda-host-ed25519"),
    ):
        key = serialization.load_ssh_private_key(
            (state_home() / "credentials" / filename).read_bytes(), password=None
        )
        values[name] = sha(
            key.public_key().public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
            )
        )
    return values


def handoff():
    """Resolve operator-only paths once; never accept them from management tasks."""
    from cluster import FOLDER, Host, load_hosts
    from cluster_models import PeerRegistry

    destination = private_dir(folder() / "handoff")
    receipt = destination / "manifest.json"
    if receipt.exists():
        record = json.loads(receipt.read_text())
        if set(record["files"]) != FILES or any(
            sha((destination / name).read_bytes()) != digest
            for name, digest in record["files"].items()
        ):
            raise ValueError("Retained executor handoff changed")
        return record
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        peers = PeerRegistry.model_validate_json((FOLDER / "peers.json").read_text())
        hosts = load_hosts()
        if set(peers.peers) != CPU_NAMES or set(hosts) != CPU_NAMES:
            raise ValueError(
                "Initial executor handoff requires exactly the six persistent CPU hosts"
            )
        uid = (
            Host(hosts["cp-01"])
            .run(
                [
                    "sudo",
                    "kubectl",
                    "--kubeconfig=/etc/kubernetes/admin.conf",
                    "get",
                    "namespace",
                    "kube-system",
                    "-o",
                    "jsonpath={.metadata.uid}",
                ]
            )
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9-]{36}", uid):
            raise ValueError("Cannot establish the live cluster's namespace identity")
        known, transported = [], {}
        for name in sorted(CPU_NAMES):
            fields, peer = hosts[name], peers.peers[name]
            if fields["ansible_host"] != peer.instance_id:
                raise ValueError("Persistent CPU transport and provider identity differ")
            args = shlex.split(fields["ansible_ssh_common_args"])
            paths = [a.split("=", 1)[1] for a in args if a.startswith("UserKnownHostsFile=")]
            if len(paths) != 1 or not Path(paths[0]).is_relative_to(state_home()):
                raise ValueError("CPU access requires an operator-owned strict host pin")
            entries = [
                line
                for line in Path(paths[0]).read_text().splitlines()
                if line.startswith(peer.instance_id + " ssh-ed25519 ")
            ]
            if len(entries) != 1:
                raise ValueError("Expected exactly one trusted CPU Ed25519 host identity")
            known.append(entries[0])
            effective = copy.deepcopy(fields)
            proxy = shlex.join(
                [
                    "aws",
                    "ssm",
                    "start-session",
                    "--target",
                    "%h",
                    "--document-name",
                    "AWS-StartSSHSession",
                    "--parameters",
                    "portNumber=%p",
                    "--profile",
                    "kubernetes-llm-foundation",
                    "--region",
                    CONFIG["region"],
                ]
            )
            effective.update(
                {
                    "ansible_ssh_private_key_file": "/run/secrets/host-executor/foundation-ssh-key",
                    "ansible_ssh_common_args": "-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes "
                    "-o UserKnownHostsFile=/executor/state/cluster/cpu-known-hosts -o ProxyCommand="
                    + shlex.quote(proxy),
                }
            )
            # Mesh does not rewrite host firewall/runtime configuration. Do not
            # carry obsolete operator-local paths into the container inventory.
            effective.pop("klip_firewall_file", None)
            transported[name] = effective
        key_path = state_home() / "credentials/lambda-host-ed25519"
        if not key_path.exists():
            key = Ed25519PrivateKey.generate()
            write_private(
                key_path,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                ).decode(),
            )
        key = serialization.load_ssh_private_key(key_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Dedicated Lambda access must use Ed25519")
        public = (
            key.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        values = {
            "reports/foundation.json": (state_home() / "reports/foundation.json").read_text(),
            "cluster/peers.json": peers.model_dump_json(indent=2),
            "cluster/ca.crt": (FOLDER / "ca.crt").read_text(),
            "cluster/cpu-known-hosts": "\n".join(known) + "\n",
            "cluster/lambda-ssh-key.pub": public + "\n",
            "cluster/inventory.yaml": yaml.safe_dump(
                {
                    "all": {
                        "vars": {
                            "klip_repository": "/app",
                            "klip_peer_registry": "/executor/state/cluster/peers.json",
                            "klip_ca_bundle": "/executor/state/cluster/ca.crt",
                        },
                        "children": {
                            "control_plane": {
                                "hosts": {
                                    k: v for k, v in transported.items() if k.startswith("cp-")
                                }
                            },
                            "external": {
                                "hosts": {
                                    k: v
                                    for k, v in transported.items()
                                    if k.startswith("platform-")
                                }
                            },
                        },
                    }
                }
            ),
            "cluster/executor-inputs.json": json.dumps(
                {
                    "schema_version": "klip-host-executor-inputs.v1",
                    "cluster_id": "klip",
                    "kube_system_uid": uid,
                    "cpu_peers": peers.model_dump(mode="json")["peers"],
                    "ssh_source_cidrs": sorted(
                        str(peers.peers[n].public_ip) + "/32"
                        for n in CPU_NAMES
                        if n.startswith("platform-")
                    ),
                },
                sort_keys=True,
            ),
            "cluster/peer-owner.json": json.dumps(
                {
                    "schema_version": "klip-peer-owner.v1",
                    "owner": "host-executor",
                    "writer_root": "/executor/state/cluster",
                }
            ),
        }
        for name, value in values.items():
            write_private(destination / name, value)
        record = {
            "schema_version": "klip-host-handoff.v1",
            "cluster_id": "klip",
            "kube_system_uid": uid,
            "created_at": utc_now(),
            "files": {name: sha(value.encode()) for name, value in values.items()},
        }
        write_private(receipt, record)
        material(folder() / "tls")
        return record


def rendered(values):
    output = subprocess.run(
        [
            tool("helm"),
            "template",
            "host-executor",
            str(ROOT / "charts/klip-host-executor"),
            "-n",
            NAMESPACE,
            "--values",
            str(values),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    documents = [d for d in yaml.safe_load_all(output) if d]
    guard_workloads(documents)
    return output, documents


def checked_image_source(review):
    # Verify every shipped executable input, while allowing unrelated operator
    # release code/tests to evolve after the immutable image was built.
    paths = [
        "apps/management/backend/src",
        "packages/platform-contracts/src",
        "scripts/infrastructure/cluster.py",
        "scripts/infrastructure/cluster_workers.py",
        "scripts/infrastructure/cluster_config.py",
        "scripts/infrastructure/cluster_models.py",
        "scripts/infrastructure/common.py",
        "scripts/infrastructure/gpu_runtime.py",
        "scripts/infrastructure/gpu_operator.py",
        "scripts/infrastructure/workload_identity.py",
        "scripts/infrastructure/issuer_transition.py",
        "scripts/infrastructure/platform_artifacts.py",
        "scripts/infrastructure/inventory.py",
        "scripts/infrastructure/cluster_verification.py",
        "scripts/infrastructure/verification.py",
        "scripts/infrastructure/image_release.py",
        "scripts/infrastructure/publish_images.py",
        "scripts/infrastructure/host_executor_image.py",
        "scripts/infrastructure/host_executor_openssh.py",
        "infra/ansible",
        "deploy/artifacts.lock.json",
        "deploy/gpu-host.lock.json",
        "deploy/gpu-operator.lock.json",
        "infra/terraform/project.json",
        "infra/terraform/platform.json",
        "infra/terraform/identity.json",
        "deploy/host-executor-aws-config",
        "uv.lock",
        "dev/host-executor/Dockerfile",
    ]
    different = subprocess.run(
        ["git", "diff", review["source_commit"], "--", *paths], capture_output=True, check=True
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *paths],
        capture_output=True,
        check=True,
    ).stdout
    if different or untracked:
        raise ValueError("Executor's shipped code/configuration differs from its reviewed image")


def prepare(receipt, security):
    review = assess(receipt, security)
    checked_image_source(review)
    previous_stages = [json.loads(p.read_text()) for p in folder().glob("*.stage.json")]
    if (folder() / "active.json").exists() or any(
        s.get("status") != "failed" or not s.get("retired_at") or not s.get("job_uid")
        for s in previous_stages
    ):
        raise ValueError(
            "Initial custody is already staged; preserve it and use explicit upgrade/recovery"
        )
    retained = handoff()
    allocation = json.loads(tf("storage", "output", "-json", "storage_manifest"))["host_executor"]
    if allocation["uid"] != 20002 or allocation["gid"] != 20002:
        raise ValueError("Executor storage is not the isolated non-root identity")
    values = {
        "image": review["image"],
        "fileSystemId": allocation["file_system_id"],
        "accessPointId": allocation["access_point_id"],
    }
    path = folder() / "values.yaml"
    write_private(path, yaml.safe_dump(values))
    output, _ = rendered(path)
    write_private(folder() / "rendered.yaml", output)
    plan = {
        "schema_version": "klip-host-release.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "image_assessment": review,
        "receipt_path": str(receipt.resolve()),
        "security_directory": str(security.resolve()),
        "handoff_sha256": sha(json.dumps(retained, sort_keys=True).encode()),
        "custody_fingerprints": custody(),
        "values_sha256": sha(path.read_bytes()),
        "rendered_sha256": sha(output.encode()),
    }
    plan["approval_sha256"] = sha(json.dumps(plan, sort_keys=True).encode())
    write_private(folder() / "plan.json", plan)
    return plan


def checked_plan(approval):
    plan = json.loads((folder() / "plan.json").read_text())
    if (
        approval != plan["approval_sha256"]
        or approval
        != sha(
            json.dumps(
                {k: v for k, v in plan.items() if k != "approval_sha256"}, sort_keys=True
            ).encode()
        )
        or plan["source_sha256"] != source_digest()
        or sha((folder() / "values.yaml").read_bytes()) != plan["values_sha256"]
        or sha(rendered(folder() / "values.yaml")[0].encode()) != plan["rendered_sha256"]
        or assess(Path(plan["receipt_path"]), Path(plan["security_directory"]))
        != plan["image_assessment"]
        or sha(json.dumps(handoff(), sort_keys=True).encode()) != plan["handoff_sha256"]
        or custody() != plan["custody_fingerprints"]
    ):
        raise ValueError("Executor source, custody, scan or saved release plan changed")
    return plan


def secret(cp, namespace, name, data):
    """Compare immutable exact-project values without logging or rotating on retry."""
    command = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "-n", namespace]
    live = json.loads(
        cp.run([*command, "get", "secret", name, "--ignore-not-found", "-o", "json"], secret=True)
        or "null"
    )
    if live:
        decoded = {k: base64.b64decode(v).decode() for k, v in live["data"].items()}
        if (
            not live.get("immutable")
            or live["metadata"].get("annotations", {}).get("klip.dev/project") != CONFIG["project"]
            or not hmac.compare_digest(
                json.dumps(decoded, sort_keys=True), json.dumps(data, sort_keys=True)
            )
        ):
            raise ValueError("Existing host custody differs; explicit rotation/recovery required")
    else:
        document = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "immutable": True,
            "metadata": {
                "name": name,
                "namespace": namespace,
                "annotations": {"klip.dev/project": CONFIG["project"]},
            },
            "stringData": data,
        }
        live = json.loads(
            cp.run(
                [*command, "create", "-f", "-", "-o", "json"],
                data=json.dumps(document),
                secret=True,
            )
        )
    return live["metadata"]["uid"]


def bootstrap_job(plan, documents):
    deployment = next(d for d in documents if d["kind"] == "Deployment")
    template = copy.deepcopy(deployment["spec"]["template"])
    identity = "host-stage-" + plan["approval_sha256"][:20]
    template["metadata"]["labels"] = {
        "app.kubernetes.io/name": "klip-host-executor",
        "app.kubernetes.io/component": "bootstrap",
        "klip.dev/host-task": identity,
    }
    pod = template["spec"]
    pod.update(restartPolicy="Never", serviceAccountName="klip-host-job")
    container = pod["containers"][0]
    container["name"] = "custody-preflight"
    # The API itself needs less memory than AWS CLI + SSM + SSH. Exercise the
    # actual host-Job envelope, rather than inheriting the API's smaller limit.
    container["resources"] = {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }
    for probe in ("startupProbe", "readinessProbe", "livenessProbe", "ports"):
        container.pop(probe, None)
    done = f"/executor/state/bootstrap/{identity}/done"
    container["command"] = [
        "python",
        "-c",
        "from pathlib import Path; import time\n"
        f"while not Path({done!r}).is_file(): time.sleep(1)\n",
    ]
    container["env"] = [v for v in container["env"] if v["name"] != "HOST_EXECUTOR_IMAGE"] + [
        {"name": "AWS_CONFIG_FILE", "value": "/app/deploy/host-executor-aws-config"},
    ]
    container["volumeMounts"] = [
        v for v in container["volumeMounts"] if v["name"] not in {"tls", "identity"}
    ] + [
        {"name": "credentials", "mountPath": "/run/secrets/host-executor", "readOnly": True},
        {"name": "identity", "mountPath": "/var/run/secrets/klip-host-identity", "readOnly": True},
    ]
    pod["volumes"] = [v for v in pod["volumes"] if v["name"] not in {"tls", "identity"}] + [
        {
            "name": "credentials",
            "secret": {"secretName": "klip-host-credentials", "defaultMode": 0o440},
        },
        {
            "name": "identity",
            "projected": {
                "defaultMode": 0o440,
                "sources": [
                    {
                        "serviceAccountToken": {
                            "audience": "sts.amazonaws.com",
                            "expirationSeconds": 900,
                            "path": "token",
                        }
                    },
                ],
            },
        },
    ]
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": identity,
            "namespace": NAMESPACE,
            "annotations": {"klip.dev/release-sha256": plan["approval_sha256"]},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "ttlSecondsAfterFinished": 86400,
            "template": template,
        },
    }
    guard_workloads([job])
    return job


STAGE_PROGRAM = """
import json,sys
from pathlib import Path
from platform_management.integrations.job_runner import frozen_file
document=json.load(sys.stdin)
allowed={'reports/foundation.json','cluster/peers.json','cluster/inventory.yaml','cluster/ca.crt',
         'cluster/cpu-known-hosts','cluster/lambda-ssh-key.pub','cluster/executor-inputs.json','cluster/peer-owner.json'}
if set(document['files']) != allowed: raise ValueError('Unexpected handoff file set')
root=Path('/executor/state')
receipt=root/'handoff.json'
encoded=json.dumps(document,sort_keys=True).encode()
if receipt.exists() and receipt.read_bytes()!=encoded: raise ValueError('Existing handoff differs')
for name,value in document['files'].items():
    if len(value.encode())>262144: raise ValueError('Handoff file exceeds bound')
    frozen_file(root/name,value.encode())
frozen_file(receipt,encoded)
print(json.dumps({'files':len(allowed),'verified':True}))
"""

PREFLIGHT_PROGRAM = """
import json
from common import state_home,utc_now
from cluster import Host,checked,load_hosts
from platform_management.host_executor.worker import scope
inputs,registry,cp=scope(state_home())
identity=json.loads(checked(['aws','sts','get-caller-identity','--profile','kubernetes-llm-foundation','--output','json'],secret=True))
if (identity['Account']!='127811062476' or not identity['Arn'].startswith(
    'arn:aws:sts::127811062476:assumed-role/klip-host-executor/')):
    raise ValueError('Unexpected SSH workload identity')
hosts=load_hosts()
if set(hosts)!={'cp-01','cp-02','cp-03','platform-01','platform-02','platform-03'}:
    raise ValueError('Unexpected persistent host inventory')
for name,fields in sorted(hosts.items()):
    Host(fields).run(['sudo','true'])
print(json.dumps({'schema_version':'klip-host-preflight.v1','verified_at':utc_now(),
    'kube_system_uid':inputs['kube_system_uid'],'registry_revision':registry.revision,
    'strict_ssh_hosts':sorted(hosts),'aws_identity':identity['Arn'],
    'ssh_keys_outside_management':True,'gpu_execution_verified':False}))
"""


def stage(approval):
    from cluster import Host, kubectl, load_hosts
    from platform_management.integrations.job_runner import frozen_file

    plan = checked_plan(approval)
    cp = Host(load_hosts()["cp-01"])
    command = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf"]
    existing = kubectl("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json")
    if not existing.strip():
        namespace = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": NAMESPACE,
                "annotations": {"klip.dev/project": CONFIG["project"]},
                "labels": {
                    **{
                        "pod-security.kubernetes.io/" + mode: "restricted"
                        for mode in ("enforce", "audit", "warn")
                    },
                    **{
                        "pod-security.kubernetes.io/" + mode + "-version": "v1.36"
                        for mode in ("enforce", "audit", "warn")
                    },
                },
            },
        }
        cp.run([*command, "create", "-f", "-"], data=json.dumps(namespace))
    live_ns = json.loads(kubectl("get", "namespace", NAMESPACE, "-o", "json"))
    if (
        live_ns["metadata"].get("annotations", {}).get("klip.dev/project") != CONFIG["project"]
        or live_ns["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce")
        != "restricted"
    ):
        raise ValueError(
            "Executor namespace has foreign ownership or insufficient admission policy"
        )
    materials = material(folder() / "tls")
    credentials = {
        "foundation-ssh-key": (state_home() / "credentials/foundation_ed25519").read_text(),
        "lambda-ssh-key": (state_home() / "credentials/lambda-host-ed25519").read_text(),
        "aws-config": "[profile kubernetes-llm-foundation]\n"
        "role_arn = arn:aws:iam::127811062476:role/"
        "kubernetes-llm-inference-platform/klip-host-executor\n"
        "web_identity_token_file = /var/run/secrets/host-executor/token\n"
        "role_session_name = klip-host-executor\nregion = eu-west-2\nduration_seconds = 900\n",
    }
    secret_uids = {
        "host": secret(cp, NAMESPACE, "klip-host-credentials", credentials),
        "server": secret(
            cp,
            NAMESPACE,
            "klip-host-server-tls",
            {n: (materials["server"] / n).read_text() for n in ("tls.key", "tls.crt", "ca.crt")},
        ),
        "client": secret(
            cp,
            "klip",
            "klip-host-client-tls",
            {n: (materials["client"] / n).read_text() for n in ("tls.key", "tls.crt", "ca.crt")},
        ),
    }
    _, documents = rendered(folder() / "values.yaml")
    for document in documents:
        if document["kind"] == "Deployment":
            continue
        metadata = document["metadata"]
        scope = [] if document["kind"] == "PersistentVolume" else ["-n", NAMESPACE]
        current = kubectl(
            *scope, "get", document["kind"], metadata["name"], "--ignore-not-found", "-o", "json"
        )
        if current.strip():
            observed = json.loads(current)["metadata"].get("annotations", {})
            if (
                observed.get("meta.helm.sh/release-name") != "host-executor"
                or observed.get("meta.helm.sh/release-namespace") != NAMESPACE
            ):
                raise ValueError("Refusing to adopt a foreign executor prerequisite")
        metadata.setdefault("annotations", {}).update(
            {
                "meta.helm.sh/release-name": "host-executor",
                "meta.helm.sh/release-namespace": NAMESPACE,
            }
        )
        metadata.setdefault("labels", {})["app.kubernetes.io/managed-by"] = "Helm"
        cp.run([*command, *scope, "apply", "-f", "-"], data=json.dumps(document))
    job = bootstrap_job(plan, documents)
    frozen_file(folder() / (approval + ".stage-job.json"), json.dumps(job, sort_keys=True).encode())
    status_path = folder() / (approval + ".stage.json")
    if status_path.exists():
        status = json.loads(status_path.read_text())
        if (
            status["secret_uids"] != secret_uids
            or status["namespace_uid"] != live_ns["metadata"]["uid"]
        ):
            raise ValueError("Previously staged executor custody changed")
        if status["status"] == "verified":
            return status
        current = kubectl(
            "-n", NAMESPACE, "get", "job", status["job_name"], "--ignore-not-found", "-o", "json"
        )
        if not current.strip() or (
            status.get("job_uid") and json.loads(current)["metadata"]["uid"] != status["job_uid"]
        ):
            raise ValueError("Original staging Job lost or replaced; it cannot be restarted")
        observed = json.loads(current)
        # A lost create response can attach only to the exact frozen native spec.
        from workload_acceptance import validate_preparation

        validate_preparation(observed, job)
        conditions = {
            c["type"]
            for c in observed.get("status", {}).get("conditions", [])
            if c.get("status") == "True"
        }
        if "Failed" in conditions:
            raise ValueError("Original staging Job failed; no automatic restart is permitted")
        if "Complete" in conditions:
            if (
                status["status"] != "preflight_verified"
                or (state_home() / "cluster/peer-owner.json").read_text()
                != (folder() / "handoff/cluster/peer-owner.json").read_text()
            ):
                raise ValueError(
                    "Completed staging Job has no retained preflight/ownership evidence"
                )
            status.update(status="verified", verified_at=utc_now())
            write_private(status_path, status)
            return status
    else:
        status = {
            "schema_version": "klip-host-stage.v1",
            "approval_sha256": approval,
            "started_at": utc_now(),
            "status": "staging",
            "namespace_uid": live_ns["metadata"]["uid"],
            "secret_uids": secret_uids,
            "job_name": job["metadata"]["name"],
        }
        write_private(status_path, status)
        observed = json.loads(
            cp.run(
                [*command, "-n", NAMESPACE, "create", "-f", "-", "-o", "json"], data=json.dumps(job)
            )
        )
    status["job_uid"] = observed["metadata"]["uid"]
    write_private(status_path, status)
    import time

    deadline = time.monotonic() + 120
    while True:
        pods = json.loads(
            kubectl(
                "-n", NAMESPACE, "get", "pods", "-l", "job-name=" + status["job_name"], "-o", "json"
            )
        )["items"]
        if pods:
            break
        if time.monotonic() >= deadline:
            raise ValueError("Original staging Job did not create a Pod")
        time.sleep(1)
    if len(pods) != 1 or not any(
        o.get("uid") == status["job_uid"] for o in pods[0]["metadata"].get("ownerReferences", [])
    ):
        raise ValueError("Staging Pod differs from the native Job identity")
    pod = pods[0]["metadata"]["name"]
    kubectl("-n", NAMESPACE, "wait", "--for=condition=Ready", "pod/" + pod, "--timeout=120s")
    files = {name: (folder() / "handoff" / name).read_text() for name in FILES}
    cp.run(
        [*command, "-n", NAMESPACE, "exec", "-i", pod, "--", "python", "-c", STAGE_PROGRAM],
        data=json.dumps({"files": files}),
        secret=True,
    )
    proof = json.loads(
        cp.run(
            [*command, "-n", NAMESPACE, "exec", pod, "--", "python", "-c", PREFLIGHT_PROGRAM],
            secret=True,
        )
    )
    if proof["kube_system_uid"] != handoff()["kube_system_uid"]:
        raise ValueError("Executor preflight reached a different cluster")
    status.update(status="preflight_verified", preflight=proof, pod_uid=pods[0]["metadata"]["uid"])
    write_private(status_path, status)
    # Fence the old operator only after mounted state and all six strict SSH paths pass.
    write_private(
        state_home() / "cluster/peer-owner.json",
        (folder() / "handoff/cluster/peer-owner.json").read_text(),
    )
    done = f"/executor/state/bootstrap/{status['job_name']}/done"
    cp.run(
        [
            *command,
            "-n",
            NAMESPACE,
            "exec",
            pod,
            "--",
            "python",
            "-c",
            "from pathlib import Path; p=Path("
            + repr(done)
            + "); p.parent.mkdir(parents=True,exist_ok=True); p.touch()",
        ]
    )
    kubectl(
        "-n",
        NAMESPACE,
        "wait",
        "--for=condition=Complete",
        "job/" + status["job_name"],
        "--timeout=30s",
    )
    status.update(status="verified", verified_at=utc_now())
    write_private(status_path, status)
    return status


def deploy(approval):
    from cluster import FOLDER, checked, kubeconfigs, tunnel

    plan = checked_plan(approval)
    active = folder() / "active.json"
    if active.exists():
        previous = json.loads(active.read_text())
        if previous["approval_sha256"] != approval:
            raise ValueError("Existing executor release needs explicit history-preserving upgrade")
        return previous
    staged = json.loads((folder() / (approval + ".stage.json")).read_text())
    if staged["status"] != "verified":
        raise ValueError("Isolated storage/SSH preflight must pass before API deployment")
    kubeconfigs()
    with (state_home() / "cluster/operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tunnel():
            output = checked(
                [
                    tool("helm"),
                    "upgrade",
                    "--install",
                    "host-executor",
                    str(ROOT / "charts/klip-host-executor"),
                    "-n",
                    NAMESPACE,
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "--values",
                    str(folder() / "values.yaml"),
                    "--atomic",
                    "--wait",
                    "--timeout",
                    "5m",
                    "--history-max",
                    "10",
                ],
                timeout=360,
            )
    write_private(folder() / "helm.log", output)
    record = {
        "approval_sha256": approval,
        "status": "deployed",
        "deployed_at": utc_now(),
        "image": plan["image_assessment"]["image"],
    }
    write_private(folder() / "active.json", record)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["handoff", "prepare", "stage", "deploy"])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--security-directory", type=Path)
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    if args.action == "handoff":
        result = handoff()
    elif args.action == "prepare":
        result = prepare(args.receipt, args.security_directory)
    else:
        result = {"stage": stage, "deploy": deploy}[args.action](args.approve_plan)
    print(json.dumps(result, indent=2))
