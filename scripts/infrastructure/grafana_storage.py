"""Independent Grafana credentials and declarative CNPG objects; never reset live secrets."""

from __future__ import annotations

import base64
import hmac
import json
import secrets

import yaml
from common import ROOT, state_home, write_private
from monitoring_runtime import get


def secret_get(name, namespace):
    from cluster import Host, load_hosts

    raw = (
        Host(load_hosts()["cp-01"])
        .run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                namespace,
                "get",
                "secret",
                name,
                "--ignore-not-found",
                "-o",
                "json",
            ],
            secret=True,
        )
        .strip()
    )
    return json.loads(raw) if raw else None


def credentials():
    path = state_home() / "credentials/grafana.json"
    if path.exists():
        value = json.loads(path.read_text())
        if (
            set(value) != {"admin-user", "admin-password", "database-password", "encryption-key"}
            or value["admin-user"] != "admin"
            or any(
                len(value[k]) < 32
                for k in ("admin-password", "database-password", "encryption-key")
            )
        ):
            raise ValueError("Unexpected retained Grafana credential structure")
        return value
    if any(
        secret_get(name, namespace)
        for name, namespace in (
            ("klip-grafana-credentials", "observability"),
            ("klip-grafana-database", "platform-data"),
        )
    ):
        raise ValueError(
            "Recover the retained Grafana credentials before touching existing Secrets"
        )
    value = {
        "admin-user": "admin",
        **{
            k: secrets.token_urlsafe(48)
            for k in ("admin-password", "database-password", "encryption-key")
        },
    }
    write_private(path, value)
    return value


def ensure_secret(name, namespace, values, *, kind="Opaque", labels=None):
    from cluster import Host, load_hosts

    existing = secret_get(name, namespace)
    if existing:
        if (
            existing.get("type") != kind
            or set(existing.get("data", {})) != set(values)
            or any(
                not hmac.compare_digest(base64.b64decode(existing["data"][k]).decode(), v)
                for k, v in values.items()
            )
        ):
            raise ValueError(
                "Live and retained Grafana secret values differ; explicit recovery required"
            )
        return {"name": name, "namespace": namespace, "uid": existing["metadata"]["uid"]}
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"klip.dev/managed-by": "klip-observability", **(labels or {})},
        },
        "type": kind,
        "stringData": values,
    }
    Host(load_hosts()["cp-01"]).run(
        ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
        data=json.dumps(doc),
        secret=True,
    )
    return {"name": name, "namespace": namespace}


def initialize():
    from cluster import Host, load_hosts
    from database import verify
    from smoke import wait_until

    value = credentials()
    db = verify()
    cp = Host(load_hosts()["cp-01"])
    existing_role = get("databaserole", "klip-grafana", "platform-data")
    existing_database = get("database", "klip-grafana", "platform-data")
    catalog = cp.run(
        [
            "sudo",
            "kubectl",
            "--kubeconfig=/etc/kubernetes/admin.conf",
            "-n",
            "platform-data",
            "exec",
            db["primary"],
            "-c",
            "postgres",
            "--",
            "psql",
            "-X",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-At",
            "-c",
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='grafana')::int, "
            "EXISTS(SELECT 1 FROM pg_database WHERE datname='grafana')::int",
        ],
    ).strip()
    if catalog not in {"0|0", "1|0", "1|1"}:
        raise ValueError("Unexpected Grafana database catalog state")
    role_present, database_present = catalog.split("|")
    if (role_present == "1" and not existing_role) or (
        database_present == "1" and not existing_database
    ):
        raise ValueError(
            "Unmanaged Grafana database/role exists; explicit restore adoption required"
        )
    ensure_secret(
        "klip-grafana-database",
        "platform-data",
        {"username": "grafana", "password": value["database-password"]},
        kind="kubernetes.io/basic-auth",
        labels={"cnpg.io/reload": "true"},
    )
    ensure_secret("klip-grafana-credentials", "observability", value)

    ca = Host(load_hosts()["cp-01"]).run(
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
    ensure_secret(
        "klip-grafana-database-ca",
        "observability",
        {"ca.crt": base64.b64decode(ca).decode()},
    )
    cp = Host(load_hosts()["cp-01"])
    for doc in yaml.safe_load_all(
        (ROOT / "deploy/observability/grafana-database.yaml").read_text()
    ):
        actual = get(doc["kind"], doc["metadata"]["name"], doc["metadata"]["namespace"])
        if actual:
            if actual["metadata"].get("labels", {}).get(
                "klip.dev/managed-by"
            ) != "klip-observability" or any(
                actual["spec"].get(k) != v for k, v in doc["spec"].items()
            ):
                raise ValueError("Refuse to adopt or alter another database/role")
        else:
            cp.run(
                ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "create", "-f", "-"],
                data=yaml.safe_dump(doc),
            )
        if not doc["apiVersion"].startswith("postgresql.cnpg.io/"):
            continue
        wait_until(
            lambda doc=doc: (
                o
                if (o := get(doc["kind"], doc["metadata"]["name"], "platform-data") or {})
                .get("status", {})
                .get("applied")
                is True
                else None
            ),
            "Grafana database object " + doc["kind"],
            timeout=120,
            interval=2,
        )
    return {
        "database_cluster_uid": db["cluster_uid"],
        "database": "grafana",
        "role": "grafana",
        "superuser": False,
        "tls_mode": "verify-full",
    }
