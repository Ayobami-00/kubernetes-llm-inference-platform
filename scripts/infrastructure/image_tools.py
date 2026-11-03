"""Build the unmodified upstream ECR helper on bounded, existing CPU capacity."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import re
import urllib.request

from common import ROOT, private_dir, source_digest, utc_now, write_private
from platform_services import PLACEMENT
from smoke import wait_until

LOCK = ROOT / "deploy/ecr-credential-provider.lock.json"
CACHE = ROOT / "artifacts/platform/ecr-credential-provider-linux-amd64"


def source_archive(lock: dict) -> bytes:
    path = ROOT / "artifacts/platform/ecr-credential-provider-source.tar.gz"
    if path.exists():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(lock["source"]["url"], timeout=60) as response:
            data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != lock["source"]["sha256"]:
        raise ValueError("Upstream source differs from its frozen checksum/download bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def verified_binary() -> bytes:
    lock = load_lock()
    if not lock.get("binary_sha256"):
        raise ValueError("Review/freeze the built helper checksum before installation")
    data = CACHE.read_bytes()
    if hashlib.sha256(data).hexdigest() != lock["binary_sha256"]:
        raise ValueError("Cached executable differs from the reviewed binary checksum")
    return data


def export_binary(kubectl, pod: str) -> bytes:
    """Bound each exec stream; read-only range retries never rerun the compiler."""
    prefix = ("-n", "klip", "exec", pod, "-c", "build", "--")
    path = "/scratch/output/ecr-credential-provider"
    size = int(kubectl(*prefix, "stat", "-c", "%s", path).strip())
    expected = kubectl(*prefix, "sha256sum", path).split()[0]
    if not 0 < size <= 96 * 1024 * 1024 or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Built helper exceeds the export bounds or has invalid metadata")
    chunks = []
    block = 1024 * 1024
    for offset in range(0, size, block):
        for attempt in range(3):
            try:
                encoded = kubectl(
                    *prefix,
                    "bash",
                    "-o",
                    "pipefail",
                    "-c",
                    f"dd if={path} bs={block} skip={offset // block} count=1 status=none | base64",
                )
                chunk = base64.b64decode("".join(encoded.split()), validate=True)
                break
            except RuntimeError:
                if attempt == 2:
                    raise
        if len(chunk) != min(block, size - offset):
            raise ValueError("Artifact range export was truncated; preserve the existing attempt")
        chunks.append(chunk)
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Exported helper differs from the compiled artifact checksum")
    return data


def load_lock() -> dict:
    value = json.loads(LOCK.read_text())
    source = value["source"]
    if (
        value.get("schema_version") != "klip-ecr-credential-provider.v1"
        or not re.fullmatch(r"1\.36\.[0-9]+", value["version"])
        or value.get("platform") != "linux/amd64"
        or not re.fullmatch(r"[a-f0-9]{40}", source["commit"])
        or source["url"]
        != "https://codeload.github.com/kubernetes/cloud-provider-aws/tar.gz/" + source["commit"]
        or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
        or not re.fullmatch(
            r"docker.io/library/golang:[a-z0-9.-]+@sha256:[a-f0-9]{64}", value["builder_image"]
        )
        or (
            value.get("binary_sha256") is not None
            and not re.fullmatch(r"[a-f0-9]{64}", value["binary_sha256"])
        )
    ):
        raise ValueError("Invalid upstream helper source/image/platform lock")
    return value


def job_document(operation: str, lock: dict) -> dict:
    if not re.fullmatch(r"[a-f0-9]{12}", operation):
        raise ValueError("Use a stable 12-character lowercase hexadecimal operation ID")
    script = r"""
set -eu
mkdir -p /scratch/source /scratch/output /scratch/tmp /scratch/home
printf '%s  %s\n' "$SOURCE_SHA256" /inputs/source.tar.gz | sha256sum -c -
tar -xzf /inputs/source.tar.gz --strip-components=1 -C /scratch/source
cd /scratch/source
go test -mod=readonly -p 2 ./cmd/ecr-credential-provider
go mod verify
go build -p 2 -mod=readonly -trimpath -buildvcs=false \
  -ldflags="-buildid= -s -w -X main.gitVersion=v$SOURCE_VERSION" \
  -o /scratch/output/ecr-credential-provider ./cmd/ecr-credential-provider
/scratch/output/ecr-credential-provider --version > /scratch/output/version.txt
sha256sum /scratch/output/ecr-credential-provider
cat /scratch/output/version.txt
touch /scratch/ready
count=0
while ! test -f /scratch/exported; do
  count=$((count+1))
  if test "$count" -gt 120; then echo 'Artifact export deadline expired' >&2; exit 42; fi
  sleep 5
done
"""
    environment = {
        "SOURCE_SHA256": lock["source"]["sha256"],
        "SOURCE_VERSION": lock["version"],
        "CGO_ENABLED": "0",
        "GOOS": "linux",
        "GOARCH": "amd64",
        "GOTOOLCHAIN": "local",
        "GOMAXPROCS": "2",
        "GOMEMLIMIT": "768MiB",
        "GOCACHE": "/scratch/gocache",
        "GOPATH": "/scratch/gopath",
        "TMPDIR": "/scratch/tmp",
        "HOME": "/scratch/home",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    container = {
        "name": "build",
        "image": lock["builder_image"],
        "command": ["bash", "-c", script],
        "env": [{"name": k, "value": v} for k, v in environment.items()],
        "securityContext": {
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "2Gi"},
            "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "10Gi"},
        },
        "volumeMounts": [
            {"name": "source", "mountPath": "/inputs", "readOnly": True},
            {"name": "scratch", "mountPath": "/scratch"},
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "klip-ecr-build-" + operation,
            "namespace": "klip",
            "labels": {"klip.dev/image-tool-operation": operation},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 1800,
            "podFailurePolicy": {
                "rules": [
                    {
                        "action": "FailJob",
                        "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
                    }
                ]
            },
            "template": {
                "metadata": {"labels": {"klip.dev/image-tool-operation": operation}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "nodeSelector": {**PLACEMENT, "kubernetes.io/hostname": "platform-01"},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                    "volumes": [
                        {"name": "source", "configMap": {"name": "klip-ecr-source-" + operation}},
                        {"name": "scratch", "emptyDir": {"sizeLimit": "8Gi"}},
                    ],
                },
            },
        },
    }


def build(operation: str) -> dict:
    from cluster import FOLDER, Host, delete_owned_resource, kubectl, load_hosts

    lock = load_lock()
    document = job_document(operation, lock)
    folder = private_dir(FOLDER / "image-tools" / operation)
    path = folder / "observation.json"
    if path.exists():
        receipt = json.loads(path.read_text())
        if receipt.get("completed_at"):
            if (
                not CACHE.is_file()
                or hashlib.sha256(CACHE.read_bytes()).hexdigest() != receipt["binary_sha256"]
            ):
                raise ValueError("Completed artifact is absent/changed; recover it explicitly")
            return receipt
        if receipt["lock"] != lock:
            raise ValueError("The tool build lock changed during an existing operation")
    else:
        receipt = {
            "schema_version": "klip-image-tool-build.v1",
            "operation": operation,
            "lock": lock,
            "source_sha256": source_digest(),
            "started_at": utc_now(),
            "cleanup_pending": True,
        }
        write_private(path, receipt)
    source = source_archive(lock)
    node = json.loads(kubectl("get", "node", "platform-01", "-o", "json"))
    pinned = json.loads((FOLDER / "platform-placement.json").read_text())["nodes"]["platform-01"]
    if (
        node["metadata"]["uid"] != pinned["uid"]
        or node["metadata"]["labels"].get(next(iter(PLACEMENT))) != "true"
        or not any(
            c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]
        )
    ):
        raise ValueError("The protected CPU build host is not its recorded Ready identity")
    cp = Host(load_hosts()["cp-01"])

    def get(kind, name):
        raw = kubectl("-n", "klip", "get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(raw) if raw.strip() else None

    def create(doc):
        return json.loads(
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
                data=json.dumps(doc),
            )
        )

    job = get("job", document["metadata"]["name"])
    if not job:
        if receipt.get("job_uid"):
            raise ValueError("Recorded build disappeared; never automatically restart the attempt")
        job = create(document)
    if (
        job["metadata"].get("labels", {}).get("klip.dev/image-tool-operation") != operation
        or job["spec"]["template"]["spec"]["containers"][0]["image"] != lock["builder_image"]
        or (receipt.get("job_uid") and receipt["job_uid"] != job["metadata"]["uid"])
    ):
        raise ValueError("Build Job ownership/identity differs from durable intent")
    receipt["job_uid"] = job["metadata"]["uid"]
    write_private(folder / "job.json", job)
    write_private(path, receipt)
    config_name = "klip-ecr-source-" + operation
    config = get("configmap", config_name)
    encoded = base64.b64encode(source).decode()
    if config:
        if config.get("binaryData", {}).get("source.tar.gz") != encoded or not any(
            o.get("uid") == receipt["job_uid"]
            for o in config["metadata"].get("ownerReferences", [])
        ):
            raise ValueError("Source ConfigMap differs from the verified input/ownership")
    else:
        config = create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": config_name,
                    "namespace": "klip",
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": document["metadata"]["name"],
                            "uid": receipt["job_uid"],
                            "controller": True,
                        }
                    ],
                },
                "immutable": True,
                "binaryData": {"source.tar.gz": encoded},
            }
        )
    receipt["source_configmap_uid"] = config["metadata"]["uid"]
    write_private(path, receipt)

    def pod_for_job():
        pods = json.loads(
            kubectl(
                "-n",
                "klip",
                "get",
                "pods",
                "-l",
                "batch.kubernetes.io/controller-uid=" + receipt["job_uid"],
                "-o",
                "json",
            )
        )["items"]
        if not pods:
            return None
        if len(pods) != 1:
            raise ValueError("The single-attempt build has unexpected Pod identities")
        return pods[0]

    def ready():
        current = get("job", document["metadata"]["name"])
        if not current or current["metadata"]["uid"] != receipt["job_uid"]:
            raise ValueError("Build Job disappeared or changed identity")
        if current.get("status", {}).get("failed"):
            raise ValueError("Upstream build failed; retain logs before cleanup")
        pod = pod_for_job()
        if pod is None:
            return None
        if pod["status"].get("phase") == "Running":
            kubectl(
                "-n",
                "klip",
                "exec",
                pod["metadata"]["name"],
                "-c",
                "build",
                "--",
                "test",
                "-f",
                "/scratch/ready",
            )
            return pod
        if pod["status"].get("phase") in {"Failed", "Succeeded"}:
            raise ValueError("Build exited before its artifact was exported")
        return None

    try:
        if not receipt.get("binary_sha256"):
            pod = wait_until(ready, "upstream ECR helper build/tests", timeout=1500, interval=10)
            pod_name = pod["metadata"]["name"]
            binary = export_binary(kubectl, pod_name)
            checksum = hashlib.sha256(binary).hexdigest()
            if binary[:4] != b"\x7fELF" or binary[18:20] != b"\x3e\x00":
                raise ValueError("Built artifact is not a Linux AMD64 ELF executable")
            if lock.get("binary_sha256") and lock["binary_sha256"] != checksum:
                raise ValueError("Rebuilt helper differs from its reviewed binary checksum")
            CACHE.write_bytes(binary)
            CACHE.chmod(0o755)
            receipt.update(
                binary_sha256=checksum,
                binary_bytes=len(binary),
                pod_uid=pod["metadata"]["uid"],
                version=kubectl(
                    "-n",
                    "klip",
                    "exec",
                    pod_name,
                    "-c",
                    "build",
                    "--",
                    "cat",
                    "/scratch/output/version.txt",
                ).strip(),
            )
            write_private(
                folder / "build.log", kubectl("-n", "klip", "logs", pod_name, "-c", "build")
            )
            write_private(path, receipt)
        elif (
            not CACHE.is_file()
            or hashlib.sha256(CACHE.read_bytes()).hexdigest() != receipt["binary_sha256"]
        ):
            raise ValueError(
                "Exported artifact changed; preserve the existing build for inspection"
            )
        pod = pod_for_job()
        if pod and pod["status"].get("phase") == "Running":
            kubectl(
                "-n",
                "klip",
                "exec",
                pod["metadata"]["name"],
                "-c",
                "build",
                "--",
                "touch",
                "/scratch/exported",
            )
        wait_until(
            lambda: (
                (get("job", document["metadata"]["name"]) or {}).get("status", {}).get("succeeded")
                == 1
            ),
            "build completion after export",
            timeout=60,
        )
        delete_owned_resource(
            "/apis/batch/v1/namespaces/klip/jobs/" + document["metadata"]["name"],
            receipt["job_uid"],
        )
        wait_until(
            lambda: (
                not get("job", document["metadata"]["name"]) and not get("configmap", config_name)
            ),
            "owned build/source cleanup",
            timeout=120,
        )
        receipt.update(completed_at=utc_now(), cleanup_pending=False)
        write_private(path, receipt)
        return receipt
    except BaseException as error:
        receipt["error_code"] = type(error).__name__
        write_private(path, receipt)
        raise


if __name__ == "__main__":
    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = build(args.operation)
    print(json.dumps(result, indent=2))
