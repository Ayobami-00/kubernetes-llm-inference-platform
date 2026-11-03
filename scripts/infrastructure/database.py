"""Reviewed, non-destructive initialization of the three-instance management database."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import secrets

import yaml
from common import (
    PROFILES,
    ROOT,
    Aws,
    private_dir,
    source_digest,
    state_home,
    tool,
    utc_now,
    write_private,
)
from database_services import postgres_image
from platform_services import digest
from smoke import wait_until

NAME = "klip-db"
NAMESPACE = "platform-data"
SECRET = "klip-db-app"


def preparation_paths():
    folder = private_dir(state_home() / "cluster/database")
    return folder, folder / "initialization.json", folder / "values.yaml"


def assert_empty_archive(aws, bucket: str, prefix: str) -> None:
    from common import CONFIG

    actual = aws.call(
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--prefix",
        prefix,
        "--expected-bucket-owner",
        CONFIG["account_id"],
        "--max-keys",
        "1",
        "--no-paginate",
    )
    if actual.get("Contents"):
        raise ValueError(
            "Archive already contains data; initialize is forbidden. Use explicit recovery."
        )


def values_for_database(storage: dict) -> dict:
    database = storage["database"]
    if database["namespace"] != NAMESPACE or NAME not in database["service_accounts"]:
        raise ValueError("Database identity is absent from the scoped storage handoff")
    return {
        "imageName": postgres_image(digest),
        "storage": {"storageClass": "klip-gp3", "size": "40Gi"},
        "walStorage": {"storageClass": "klip-gp3", "size": "20Gi"},
        "applicationSecret": SECRET,
        "backup": {
            "authentication": "webIdentity",
            "credentialsSecret": "",
            "roleArn": database["role_arn"],
            "destinationPath": "s3://"
            + database["bucket"]
            + "/"
            + database["archive_prefix"]
            + "/management",
        },
        "networkPolicy": {"managementNamespace": "klip"},
    }


def prepare() -> dict:
    from cluster import kubectl
    from platform_storage import verify_storage

    aws = Aws(PROFILES["storage"])
    aws.preflight("storage")
    existing = kubectl("-n", NAMESPACE, "get", "cluster", NAME, "--ignore-not-found", "-o", "json")
    if existing.strip():
        raise ValueError(
            "Database already exists; use verify or an explicit upgrade/recovery procedure"
        )
    storage = verify_storage()["allocation"]
    assert_empty_archive(
        aws, storage["database"]["bucket"], storage["database"]["archive_prefix"] + "/management/"
    )
    namespace = json.loads(kubectl("get", "namespace", NAMESPACE, "-o", "json"))
    storageclass = json.loads(kubectl("get", "storageclass", "klip-gp3", "-o", "json"))
    if (
        storageclass.get("reclaimPolicy") != "Retain"
        or storageclass.get("volumeBindingMode") != "WaitForFirstConsumer"
        or storageclass["parameters"].get("encrypted") != "true"
    ):
        raise ValueError("The database requires the verified encrypted retained storage class")
    values = values_for_database(storage)
    folder, receipt_path, values_path = preparation_paths()
    write_private(values_path, yaml.safe_dump(values))
    receipt = {
        "schema_version": "klip-database-initialization.v1",
        "created_at": utc_now(),
        "mode": "initdb",
        "cluster": NAME,
        "namespace": NAMESPACE,
        "namespace_uid": namespace["metadata"]["uid"],
        "storageclass_uid": storageclass["metadata"]["uid"],
        "source_sha256": source_digest(),
        "values_sha256": hashlib.sha256(values_path.read_bytes()).hexdigest(),
        "archive_bucket": storage["database"]["bucket"],
        "archive_prefix": storage["database"]["archive_prefix"] + "/management/",
        "resource_envelope": {
            "instances": 3,
            "data_gib_each": 40,
            "wal_gib_each": 20,
            "gp3_gib_total": 180,
            "gp3_monthly_usd_at_reviewed_rate": 16.704,
        },
    }
    receipt["approval_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True).encode()
    ).hexdigest()
    write_private(receipt_path, receipt)
    return receipt


def application_secret() -> dict:
    """Retain and compare existing credentials; never regenerate over a live Secret."""
    from cluster import Host, kubectl, load_hosts
    from common import CONFIG

    path = state_home() / "credentials/management-postgresql.json"
    present = kubectl(
        "-n",
        NAMESPACE,
        "get",
        "secret",
        SECRET,
        "--ignore-not-found",
        "-o",
        "jsonpath={.metadata.uid}",
    ).strip()
    if path.exists():
        value = json.loads(path.read_text())
        if (
            value.get("cluster_id") != CONFIG["cluster_id"]
            or value.get("namespace") != NAMESPACE
            or value.get("username") != "management"
            or not value.get("password")
        ):
            raise ValueError("Retained database credentials have an unexpected identity")
    else:
        if present:
            raise ValueError(
                "An existing database Secret needs recovery; refusing to replace its credentials"
            )
        value = {
            "cluster_id": CONFIG["cluster_id"],
            "namespace": NAMESPACE,
            "username": "management",
            "password": secrets.token_urlsafe(48),
        }
        write_private(path, value)
    cp = Host(load_hosts()["cp-01"])
    if present:
        # Secret-bearing responses stay in memory and never become command arguments/logs.
        actual = json.loads(
            cp.run(
                [
                    "sudo",
                    "kubectl",
                    "--kubeconfig=/etc/kubernetes/admin.conf",
                    "-n",
                    NAMESPACE,
                    "get",
                    "secret",
                    SECRET,
                    "-o",
                    "json",
                ],
                secret=True,
            )
        )
        for field in ("username", "password"):
            if not hmac.compare_digest(
                base64.b64decode(actual["data"][field]).decode(), value[field]
            ):
                raise ValueError(
                    "Live and retained database credentials differ; explicit recovery required"
                )
    else:
        doc = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "kubernetes.io/basic-auth",
            "metadata": {"name": SECRET, "namespace": NAMESPACE},
            "stringData": {k: value[k] for k in ("username", "password")},
        }
        cp.run(
            ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
            data=json.dumps(doc),
            secret=True,
        )
    return {"name": SECRET, "credential_reference": "credential://klip/management-postgresql"}


def validate_receipt(receipt: dict, approval: str, current_source: str, values: bytes) -> None:
    observations = {
        "approval_sha256",
        "mutation_started_at",
        "application_secret",
        "verified_at",
        "cluster_uid",
    }
    frozen = {key: value for key, value in receipt.items() if key not in observations}
    expected = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    if (
        not approval
        or receipt.get("approval_sha256") != approval
        or expected != approval
        or receipt.get("mode") != "initdb"
    ):
        raise ValueError("Approve the exact unchanged initialization receipt")
    if (
        receipt["source_sha256"] != current_source
        or receipt["values_sha256"] != hashlib.sha256(values).hexdigest()
    ):
        raise ValueError("Initialization source/values changed; prepare and review again")


def initialize(approval: str) -> dict:
    from cluster import FOLDER, checked, kubeconfigs, kubectl, tunnel
    from platform_checks import load_schemas, validate_custom_resources

    folder, receipt_path, values_path = preparation_paths()
    receipt = json.loads(receipt_path.read_text())
    validate_receipt(receipt, approval, source_digest(), values_path.read_bytes())
    aws = Aws(PROFILES["storage"])
    aws.preflight("storage")
    namespace = json.loads(kubectl("get", "namespace", NAMESPACE, "-o", "json"))
    storageclass = json.loads(kubectl("get", "storageclass", "klip-gp3", "-o", "json"))
    if (
        namespace["metadata"]["uid"] != receipt["namespace_uid"]
        or storageclass["metadata"]["uid"] != receipt["storageclass_uid"]
    ):
        raise ValueError("Database namespace/storage identity changed after review")
    existing = kubectl(
        "-n", NAMESPACE, "get", "cluster", NAME, "--ignore-not-found", "-o", "json"
    ).strip()
    if existing:
        if not receipt.get("mutation_started_at"):
            raise ValueError(
                "A database appeared after review; refusing to adopt or reinitialize it"
            )
        current = json.loads(existing)
        if current["metadata"].get("annotations", {}).get("meta.helm.sh/release-name") != NAME:
            raise ValueError("Database ownership differs; refusing to adopt it")
        if current["spec"]["imageName"] != yaml.safe_load(values_path.read_text())["imageName"]:
            raise ValueError("Existing database image differs from frozen initialization")
        return verify()
    assert_empty_archive(aws, receipt["archive_bucket"], receipt["archive_prefix"])
    secret = application_secret()
    rendered = checked(
        [
            tool("helm"),
            "template",
            NAME,
            str(ROOT / "charts/klip-database"),
            "--namespace",
            NAMESPACE,
            "--values",
            str(values_path),
        ]
    )
    validate_custom_resources([v for v in yaml.safe_load_all(rendered) if v], load_schemas())
    write_private(folder / "rendered.yaml", rendered)
    # Durable intent precedes the API mutation. Retrying never launches another cluster identity.
    receipt.update(mutation_started_at=utc_now(), application_secret=secret)
    write_private(receipt_path, receipt)
    kubeconfigs()
    with tunnel():
        output = checked(
            [
                tool("helm"),
                "upgrade",
                "--install",
                NAME,
                str(ROOT / "charts/klip-database"),
                "--namespace",
                NAMESPACE,
                "--kubeconfig",
                str(FOLDER / "operator.conf"),
                "--values",
                str(values_path),
                "--wait",
                "--timeout",
                "10m",
                "--history-max",
                "10",
            ],
            timeout=750,
        )
    write_private(folder / "helm.log", output)
    wait_until(
        lambda: (
            json.loads(kubectl("-n", NAMESPACE, "get", "cluster", NAME, "-o", "json"))
            .get("status", {})
            .get("readyInstances")
            == 3
        ),
        "three PostgreSQL instances",
        timeout=900,
        interval=5,
    )
    result = verify()
    receipt["verified_at"] = utc_now()
    receipt["cluster_uid"] = result["cluster_uid"]
    write_private(receipt_path, receipt)
    return result


def verify(name: str = NAME) -> dict:
    from cluster import kubectl

    if name not in {NAME, "klip-db-restore"}:
        raise ValueError("Unreviewed database identity")
    current = json.loads(kubectl("-n", NAMESPACE, "get", "cluster", name, "-o", "json"))
    if current.get("status", {}).get("readyInstances") != 3 or not any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in current.get("status", {}).get("conditions", [])
    ):
        raise ValueError("The management database is not three-instance Ready")
    pods = json.loads(
        kubectl(
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            "cnpg.io/cluster=" + name + ",cnpg.io/podRole=instance",
            "-o",
            "json",
        )
    )["items"]
    if len(pods) != 3 or len({p["spec"]["nodeName"] for p in pods}) != 3:
        raise ValueError("Database instances do not occupy three distinct hosts")
    result = {
        "schema_version": "klip-database-observation.v1",
        "observed_at": utc_now(),
        "source_sha256": source_digest(),
        "cluster_uid": current["metadata"]["uid"],
        "cluster": name,
        "primary": current["status"]["currentPrimary"],
        "ready_instances": 3,
        "instances": [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "node": p["spec"]["nodeName"],
            }
            for p in pods
        ],
        "not_covered_by_this_check": [
            "external WAL/archive continuity",
            "backup restoration",
            "application failover/reconnection",
            "management data migration",
        ],
    }
    filename = "observation.json" if name == NAME else "observation-restore.json"
    write_private(preparation_paths()[0] / filename, result)
    return result


if __name__ == "__main__":
    from cluster import FOLDER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "initialize", "verify"])
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    private_dir(FOLDER)
    with (FOLDER / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = (
            prepare()
            if args.command == "prepare"
            else initialize(args.approve_plan)
            if args.command == "initialize"
            else verify()
        )
    print(json.dumps(result, indent=2))
