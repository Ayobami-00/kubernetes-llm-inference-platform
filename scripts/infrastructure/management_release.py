"""Stage private credentials and deploy the reviewed HA application on CPU workers."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

import yaml
from common import (
    CONFIG,
    ROOT,
    private_dir,
    source_digest,
    state_home,
    tool,
    utc_now,
    write_private,
)
from image_credentials import healthy_targets
from image_release import assess, guard_workloads

AUTH = "klip-management-auth-v1"
DATABASE = "klip-management-database-v1"
CA = "klip-db-ca"
ORIGIN = "https://management." + CONFIG["domain"]


def folder() -> Path:
    return private_dir(state_home() / "cluster/management")


def read_secrets() -> dict[str, dict[str, str]]:
    source = ROOT / "dev/.secrets"
    admin = (source / "admin_password").read_text().strip()
    key = (source / "lambda_api_key").read_text().strip()
    db = json.loads((state_home() / "credentials/management-postgresql.json").read_text())
    if (
        len(admin) < 16
        or not key
        or db.get("username") != "management"
        or db.get("cluster_id") != CONFIG["cluster_id"]
        or len(db.get("password", "")) < 32
    ):
        raise ValueError("Existing application credential references are incomplete or invalid")
    url = "postgresql+psycopg://management:" + quote(db["password"], safe="")
    url += "@klip-db-rw.platform-data.svc:5432/management?sslmode=verify-full"
    return {AUTH: {"admin-password": admin, "lambda-api-key": key}, DATABASE: {"database-url": url}}


def initialize_admin() -> dict:
    from cluster import kubectl

    path = ROOT / "dev/.secrets/admin_password"
    if path.exists():
        if len(path.read_text().strip()) < 16:
            raise ValueError("Existing admin credential requires explicit rotation")
        return {"credential_reference": "dev/.secrets/admin_password", "created": False}
    if (
        kubectl("-n", "klip", "get", "secret", AUTH, "--ignore-not-found", "-o", "name").strip()
        or kubectl(
            "-n", "klip", "get", "deployments", "-l", "app.kubernetes.io/name=klip", "-o", "name"
        ).strip()
    ):
        raise ValueError(
            "Existing management needs credential recovery; initialization is forbidden"
        )
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(secrets.token_urlsafe(48) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return {"credential_reference": "dev/.secrets/admin_password", "created": True}


def stage() -> dict:
    from cluster import Host, kubectl, load_hosts
    from database import application_secret, verify
    from database_recovery import sql

    targets = healthy_targets()
    db = verify()
    application_secret()  # Compare retained credentials with the live database Secret.
    active = int(
        sql(
            "klip-db",
            "SELECT count(*) FROM public.jobs "
            "WHERE state NOT IN ('completed','failed','cancelled');",
        )
    )
    if active and not (folder() / "active.json").exists():
        raise ValueError("Review unfinished legacy jobs before starting production workers")
    values = read_secrets()
    cp = Host(load_hosts()["cp-01"])
    public_ca = cp.run(
        [
            "sudo",
            "kubectl",
            "--kubeconfig=/etc/kubernetes/admin.conf",
            "-n",
            "platform-data",
            "get",
            "secret",
            "klip-db-ca",
            "-o",
            r"jsonpath={.data.ca\.crt}",
        ],
        secret=True,
    )
    ca = base64.b64decode(public_ca, validate=True).decode()
    if not ca.startswith("-----BEGIN CERTIFICATE-----") or "PRIVATE KEY" in ca:
        raise ValueError("Database CA reference did not contain a public certificate")
    values[CA] = {"ca.crt": ca}
    namespace = json.loads(kubectl("get", "namespace", "klip", "-o", "json"))
    record = {
        "schema_version": "klip-management-credentials.v1",
        "started_at": utc_now(),
        "namespace_uid": namespace["metadata"]["uid"],
        "database_uid": db["cluster_uid"],
        "targets": targets,
        "secret_uids": {},
    }
    path = folder() / "credentials.json"
    if path.exists():
        previous = json.loads(path.read_text())
        if any(previous[k] != record[k] for k in ("namespace_uid", "database_uid")):
            raise ValueError("Application credential target identity changed; use recovery")
        record = previous
    write_private(path, record)
    for name, data in values.items():
        # UID-only discovery never exposes Secret values to ordinary diagnostics.
        present = kubectl(
            "-n", "klip", "get", "secret", name, "--ignore-not-found", "-o", "name"
        ).strip()
        args = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf"]
        if present:
            live = json.loads(
                cp.run([*args, "-n", "klip", "get", "secret", name, "-o", "json"], secret=True)
            )
            decoded = {k: base64.b64decode(v).decode() for k, v in live["data"].items()}
            if (
                live["metadata"].get("annotations", {}).get("klip.dev/project") != CONFIG["project"]
                or not live.get("immutable")
                or not hmac.compare_digest(
                    json.dumps(decoded, sort_keys=True), json.dumps(data, sort_keys=True)
                )
            ):
                raise ValueError(
                    "Existing application Secret differs; explicit rotation/recovery required"
                )
        else:
            if name in record["secret_uids"]:
                raise ValueError("A previously staged Secret is missing; use explicit recovery")
            doc = {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "immutable": True,
                "metadata": {
                    "name": name,
                    "namespace": "klip",
                    "annotations": {"klip.dev/project": CONFIG["project"]},
                },
                "stringData": data,
            }
            live = json.loads(
                cp.run(
                    [*args, "create", "-f", "-", "-o", "json"], data=json.dumps(doc), secret=True
                )
            )
        uid = live["metadata"]["uid"]
        if record["secret_uids"].get(name, uid) != uid:
            raise ValueError("Staged application Secret UID changed")
        record["secret_uids"][name] = uid
        write_private(path, record)
    patch = [{"op": "test", "path": "/metadata/uid", "value": record["namespace_uid"]}]
    for mode in ("enforce", "audit", "warn"):
        for key, value in ((mode, "restricted"), (mode + "-version", "v1.36")):
            label = "pod-security.kubernetes.io/" + key
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/labels/" + label.replace("/", "~1"),
                    "value": value,
                }
            )
    if not namespace["metadata"].get("labels"):
        patch.insert(1, {"op": "add", "path": "/metadata/labels", "value": {}})
    kubectl("patch", "namespace", "klip", "--type=json", "-p", json.dumps(patch))
    record["verified_at"] = utc_now()
    write_private(path, record)
    return record


def values_for(assessment: dict) -> dict:
    values = yaml.safe_load((ROOT / "deploy/environments/lab/management.yaml").read_text())
    values["backend"] = {"image": assessment["images"]["backend"]["image"]}
    values.setdefault("web", {})["image"] = assessment["images"]["web"]["image"]
    values["auth"] = {"existingSecret": AUTH}
    values["database"].update(existingSecret=DATABASE, caSecret=CA)
    values["artifacts"] = {"existingClaim": "klip-artifacts"}
    values["publicOrigin"] = ORIGIN
    values["imagePullRoleArn"] = (
        f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-images-pull"
    )
    values["runner"].update(
        image=assessment["images"]["runner"]["image"],
        simulationAdapter="kubernetes-workload-stub:v1",
        realAdapter="kubernetes:v1",
        command=["workload-runner", "execute", "--manifest", "{attempt_dir}/manifest.json"],
    )
    return values


def render(values: dict) -> str:
    return subprocess.run(
        [
            tool("helm"),
            "template",
            "klip",
            str(ROOT / "charts/klip"),
            "-n",
            "klip",
            "--kube-version",
            "1.36.5",
            "--values",
            "-",
        ],
        input=yaml.safe_dump(values),
        text=True,
        capture_output=True,
        check=True,
    ).stdout


def retain_active_release(root):
    """Preserve the verified release before preparing a different HA application image."""
    from platform_management.integrations.job_runner import frozen_file

    if not (root / "active.json").exists():
        return
    active = json.loads((root / "active.json").read_text())
    plan = json.loads((root / "plan.json").read_text())
    if (
        active["approval_sha256"] != plan["approval_sha256"]
        or hashlib.sha256((root / "values.yaml").read_bytes()).hexdigest() != plan["values_sha256"]
        or hashlib.sha256((root / "rendered.yaml").read_bytes()).hexdigest()
        != plan["manifest_sha256"]
    ):
        raise ValueError(
            "Reconcile the previous management release before replacing its saved plan"
        )
    destination = root / "release-history" / active["approval_sha256"]
    for name in ("active.json", "plan.json", "values.yaml", "rendered.yaml"):
        frozen_file(destination / name, (root / name).read_bytes())
    receipt = root / (active["approval_sha256"] + ".json")
    if receipt.exists():
        frozen_file(destination / "deployment.json", receipt.read_bytes())


def external_capacity_guard():
    """An explicit read-only mode may disable allocation only after quiescence."""
    from cluster import Host, kubectl, load_hosts

    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    gpu = [n for n in nodes if str(n["spec"].get("providerID", "")).startswith("lambda:///")]
    if (
        len(gpu) != 1
        or gpu[0]["spec"]["providerID"] != "lambda:///8f2e29b7279b42148ffaaed4a4dcdd8e"
    ):
        raise ValueError("External-only mode requires exactly the user-retained A10")
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
        raise ValueError("Read-only management inspection requires a healthy API")
    program = """import json
from sqlalchemy import select
from platform_management.db.models import Resource
from platform_management.db.session import session
with session() as db:
    rows=[]
    for r in db.scalars(select(Resource).where(Resource.kind=='worker_group')):
        if r.payload.get('mode')=='real' and r.payload.get('ownership')!='external':
            state=r.payload['group']['status']
            if r.active_operation_id or state['counts']['allocated_instances']:
                raise ValueError('Managed GPU operations/capacity must be quiescent')
            rows.append({'id':r.id,'phase':state['phase'],'allocated':0,'active_operation':False})
    print(json.dumps(sorted(rows,key=lambda r:r['id'])))
"""
    rows = json.loads(
        Host(load_hosts()["cp-01"]).run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "klip",
                "exec",
                ready[0]["metadata"]["name"],
                "-c",
                "api",
                "--",
                "python",
                "-c",
                program,
            ],
            secret=True,
        )
    )
    return {"retained_node_uid": gpu[0]["metadata"]["uid"], "managed_groups": rows}


def prepare(receipts: Path, *, external_capacity_only=False) -> dict:
    from cluster import kubectl
    from lambda_lifecycle_activation import configuration as lifecycle_configuration

    source = json.loads(next(receipts.rglob("backend.json")).read_text())["source_commit"]
    # Infrastructure can evolve while retaining a tested application image. All
    # executable/application inputs must still match that image's source commit.
    subprocess.run(["git", "merge-base", "--is-ancestor", source, "HEAD"], cwd=ROOT, check=True)
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
            "pyproject.toml",
            "uv.lock",
            "configs",
            "datasets/manifests",
            "contracts/fixtures",
            ".dockerignore",
            "scripts/management/stage_workspace.py",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    assessed = assess(receipts.resolve(), source)
    staged = json.loads((folder() / "credentials.json").read_text())
    namespace = json.loads(kubectl("get", "namespace", "klip", "-o", "json"))
    if namespace["metadata"]["uid"] != staged["namespace_uid"]:
        raise ValueError("Management namespace changed")
    targets = healthy_targets()
    values = values_for(assessed)
    external_guard = external_capacity_guard() if external_capacity_only else None
    lifecycle = None if external_capacity_only else lifecycle_configuration()
    if lifecycle is not None:
        values["lifecycle"] = lifecycle["chart_values"]
    elif external_capacity_only:
        values["lifecycle"] = {"enabled": False}
        # Explicit A10-only experiment overlay; ordinary management installations
        # keep their generic runner and need no experiment-specific Secret.
        overlay = yaml.safe_load(
            (ROOT / "deploy/environments/lab/experiments/a10/management-values.yaml").read_text()
        )
        values["runner"].update(overlay["runner"])
        values["experiments"] = overlay["experiments"]
    manifest = render(values)
    guard_workloads([d for d in yaml.safe_load_all(manifest) if d])
    retain_active_release(folder())
    write_private(folder() / "values.yaml", yaml.safe_dump(values))
    write_private(folder() / "rendered.yaml", manifest)
    plan = {
        "schema_version": "klip-management-release.v1",
        "created_at": utc_now(),
        "source_sha256": source_digest(),
        "namespace_uid": staged["namespace_uid"],
        "credential_uids": staged["secret_uids"],
        "lifecycle_binding": lifecycle,
        "external_capacity_only": external_capacity_only,
        "external_capacity_guard": external_guard,
        "targets": targets,
        "values_sha256": hashlib.sha256(yaml.safe_dump(values).encode()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
        "receipts": str(receipts.resolve()),
        "image_assessment": assessed,
        "origin": ORIGIN,
        "exposure": "ClusterIP HTTPS; external resilient frontend is a separate step",
    }
    plan["approval_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    write_private(folder() / "plan.json", plan)
    return plan


def deploy(approval: str) -> dict:
    from cluster import FOLDER, Host, checked, kubeconfigs, kubectl, load_hosts, tunnel
    from lambda_lifecycle_activation import configuration as lifecycle_configuration

    root = folder()
    plan = json.loads((root / "plan.json").read_text())
    signed = {k: v for k, v in plan.items() if k != "approval_sha256"}
    if (
        not approval
        or approval != plan["approval_sha256"]
        or approval != hashlib.sha256(json.dumps(signed, sort_keys=True).encode()).hexdigest()
        or plan["source_sha256"] != source_digest()
        or plan["targets"] != healthy_targets()
        or hashlib.sha256((root / "values.yaml").read_bytes()).hexdigest() != plan["values_sha256"]
    ):
        raise ValueError("Management release/target inputs changed after review")
    fresh = assess(Path(plan["receipts"]), plan["image_assessment"]["source_commit"])
    if fresh != plan["image_assessment"]:
        raise ValueError("Image scan/assessment changed after review")
    if plan.get("external_capacity_only"):
        if plan.get("lifecycle_binding") is not None or external_capacity_guard() != plan.get(
            "external_capacity_guard"
        ):
            raise ValueError("External capacity guard changed after planning")
        if (
            yaml.safe_load((root / "values.yaml").read_text()).get("lifecycle", {}).get("enabled")
            is not False
        ):
            raise ValueError("External-only release must disable provider lifecycle mutations")
    elif lifecycle_configuration() != plan.get("lifecycle_binding"):
        raise ValueError("Lifecycle custody or runtime approval changed after planning")
    credentials = stage()  # Recheck every value/UID without rewriting any Secret.
    if credentials["secret_uids"] != plan["credential_uids"]:
        raise ValueError("Management credential identities changed")
    # The serving observer Role is scoped to an existing, separate namespace.
    if not kubectl("get", "namespace", "llm-d", "--ignore-not-found", "-o", "name").strip():
        Host(load_hosts()["cp-01"]).run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "create",
                "namespace",
                "llm-d",
            ]
        )
    kubeconfigs()
    receipt_path = root / (approval + ".json")
    receipt = {"approval_sha256": approval, "started_at": utc_now(), "status": "running"}
    write_private(receipt_path, receipt)
    try:
        with tunnel():
            output = checked(
                [
                    tool("helm"),
                    "upgrade",
                    "--install",
                    "klip",
                    str(ROOT / "charts/klip"),
                    "-n",
                    "klip",
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "--values",
                    str(root / "values.yaml"),
                    "--atomic",
                    "--wait",
                    "--timeout",
                    "10m",
                    "--history-max",
                    "10",
                ],
                timeout=750,
            )
        write_private(root / "helm.log", output)
        receipt.update(status="deployed", deployed_at=utc_now())
        write_private(
            root / "active.json",
            {"approval_sha256": approval, "deployed_at": receipt["deployed_at"]},
        )
    except Exception:
        receipt.update(status="failed", failed_at=utc_now())
        raise
    finally:
        write_private(receipt_path, receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init-admin", "stage", "prepare", "deploy"])
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--approve-plan")
    parser.add_argument("--external-capacity-only", action="store_true")
    args = parser.parse_args()
    with (private_dir(state_home() / "cluster") / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = (
            initialize_admin()
            if args.action == "init-admin"
            else stage()
            if args.action == "stage"
            else prepare(args.receipts, external_capacity_only=args.external_capacity_only)
            if args.action == "prepare"
            else deploy(args.approve_plan)
        )
        print(json.dumps(result, indent=2))
