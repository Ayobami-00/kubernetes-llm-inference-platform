"""HA deployment policy and verified database transport, independent of cloud access."""

import json
from pathlib import Path
from shutil import which
from subprocess import run
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def render(chart, overrides=(), *, success=True):
    helm = which("helm")
    if not helm:
        pytest.skip("Helm is installed by the infrastructure verification workflow")
    command = [
        helm,
        "template",
        "klip" if chart == "klip" else "klip-db",
        str(ROOT / "charts" / chart),
        "--namespace",
        "klip",
        "--kube-version",
        "1.36.5",
    ]
    if chart == "klip":
        command += ["-f", str(ROOT / "deploy/environments/lab/management.yaml")]
        values = [
            "backend.image=registry.example/backend@sha256:" + "1" * 64,
            "web.image=registry.example/web@sha256:" + "2" * 64,
            "auth.existingSecret=auth-fixture",
            "database.existingSecret=db-fixture",
            "publicOrigin=https://management.example.org",
            "artifacts.existingClaim=artifacts-fixture",
        ]
    else:
        values = [
            "imageName=registry.example/postgres@sha256:" + "3" * 64,
            "storage.storageClass=gp3-fixture",
            "walStorage.storageClass=gp3-fixture",
            "applicationSecret=app-fixture",
            "backup.destinationPath=s3://backups-fixture/database",
            "backup.credentialsSecret=aws-fixture",
        ]
    for value in [*values, *overrides]:
        command += ["--set", value]
    process = run(command, capture_output=True, text=True, check=False)
    if not success:
        assert process.returncode != 0
        return process.stderr
    assert process.returncode == 0, process.stderr
    return [value for value in yaml.safe_load_all(process.stdout) if value]


def test_management_spreads_replicas_and_preserves_service_during_disruption():
    resources = render("klip")
    budgets = {
        item["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"]: item
        for item in resources
        if item["kind"] == "PodDisruptionBudget"
    }
    assert set(budgets) == {"api", "worker", "web"}
    deployments = [item for item in resources if item["kind"] == "Deployment"]
    for deployment in deployments:
        spec = deployment["spec"]
        assert spec["replicas"] == 2
        assert spec["strategy"]["rollingUpdate"] == {"maxSurge": 0, "maxUnavailable": 1}
        pod = spec["template"]["spec"]
        assert pod["nodeSelector"] == {"klip.dev.node-restriction.kubernetes.io/platform": "true"}
        assert not pod["tolerations"]
        anti = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        assert anti[0]["topologyKey"] == "kubernetes.io/hostname"
        assert anti[0]["labelSelector"]["matchLabels"] == spec["selector"]["matchLabels"]
        assert pod["topologySpreadConstraints"][0]["whenUnsatisfiable"] == "DoNotSchedule"
        role = spec["selector"]["matchLabels"]["app.kubernetes.io/component"]
        assert budgets[role]["spec"]["minAvailable"] == 1


@pytest.mark.parametrize(
    "override,reason",
    [
        ("replicas.api=1", "HA"),
        ("replicas.worker=1", "HA"),
        ("replicas.web=1", "HA"),
        ("availability.minAvailable=0", "minAvailable"),
        ("availability.minAvailable=2", "minAvailable"),
        ("database.requireTLS=false", "database.requireTLS"),
        ("database.caSecret=", "database.caSecret"),
        ("backend.image=backend:latest", "digest-pinned"),
    ],
)
def test_unsafe_ha_configuration_is_rejected(override, reason):
    assert reason in render("klip", [override], success=False)


def test_database_tls_applies_to_migration_api_and_worker():
    for resource in render("klip"):
        if resource["kind"] not in {"Deployment", "Job"} or resource["metadata"]["name"].endswith(
            "-web"
        ):
            continue
        pod = resource["spec"]["template"]["spec"]
        container = pod["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["MANAGEMENT_DATABASE_REQUIRE_TLS"] == "true"
        assert env["MANAGEMENT_DATABASE_CA_FILE"] == "/run/secrets/database/ca.crt"
        if resource["kind"] == "Deployment":
            assert json.loads(env["MANAGEMENT_RUNNER_NODE_SELECTOR"]) == {
                "klip.dev.node-restriction.kubernetes.io/platform": "true"
            }
            assert json.loads(env["MANAGEMENT_RUNNER_TOLERATIONS"]) == []
        volume = next(item for item in pod["volumes"] if item["name"] == "database-ca")
        assert volume["secret"]["secretName"] == "klip-db-ca"
        assert next(item for item in container["volumeMounts"] if item["name"] == "database-ca")[
            "readOnly"
        ]


def test_postgres_has_independent_storage_synchronous_replication_and_external_backups():
    resources = render("klip-database")
    cluster = next(item for item in resources if item["kind"] == "Cluster")
    spec = cluster["spec"]
    assert cluster["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert spec["instances"] == 3
    assert spec["affinity"]["podAntiAffinityType"] == "required"
    assert spec["enableSuperuserAccess"] is False
    assert spec["storage"]["size"] == "40Gi"
    assert spec["walStorage"]["size"] == "20Gi"
    assert spec["postgresql"]["synchronous"] == {
        "method": "any",
        "number": 1,
        "dataDurability": "required",
        "failoverQuorum": True,
    }
    assert spec["plugins"][0]["isWALArchiver"]
    assert spec["postgresql"]["parameters"]["archive_timeout"] == "60s"
    assert "backup" not in spec  # No deprecated in-tree Barman configuration.
    store = next(item for item in resources if item["kind"] == "ObjectStore")
    assert store["spec"]["instanceSidecarConfiguration"]["resources"]["limits"]["memory"] == "512Mi"
    credentials = store["spec"]["configuration"]["s3Credentials"]
    assert all(value["name"] == "aws-fixture" for value in credentials.values())
    assert (
        next(item for item in resources if item["kind"] == "ScheduledBackup")["spec"]["method"]
        == "plugin"
    )


def test_restore_preserves_original_archive_and_cannot_initialize_an_empty_database():
    resources = render(
        "klip-database",
        [
            "bootstrap.mode=recovery",
            "applicationSecret=management-db-app",
            "bootstrap.recovery.destinationPath=s3://backups-fixture/old-database",
            "bootstrap.recovery.serverName=previous-cluster",
        ],
    )
    cluster = next(item for item in resources if item["kind"] == "Cluster")
    assert cluster["spec"]["bootstrap"] == {
        "recovery": {
            "source": "source",
            "database": "management",
            "owner": "management",
            "secret": {"name": "management-db-app"},
        }
    }
    assert cluster["spec"]["externalClusters"][0]["plugin"]["parameters"] == {
        "barmanObjectName": "klip-db-source",
        "serverName": "previous-cluster",
    }
    stores = [item for item in resources if item["kind"] == "ObjectStore"]
    assert len(stores) == 2
    source = next(item for item in stores if item["metadata"]["name"].endswith("-source"))
    assert "retentionPolicy" not in source["spec"]
    message = render(
        "klip-database",
        [
            "bootstrap.mode=recovery",
            "bootstrap.recovery.destinationPath=s3://backups-fixture/database",
            "bootstrap.recovery.serverName=klip-db",
        ],
        success=False,
    )
    assert "preserve the source archive" in message


@pytest.mark.parametrize(
    "override", ["instances=1", "imageName=postgres:latest", "bootstrap.mode=x"]
)
def test_incomplete_database_deployments_fail_closed(override):
    render("klip-database", [override], success=False)


def test_database_transport_requires_verified_tls_and_recovers_stale_pool_connections(
    monkeypatch, tmp_path
):
    from platform_management.db import session as database
    from platform_management.settings import Settings

    ca = tmp_path / "ca.crt"
    ca.write_text("synthetic CA file; no connection is made")
    settings = Settings(database_require_tls=True, database_ca_file=ca)
    create = Mock()
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    monkeypatch.setattr(database, "create_engine", create)
    database.engine.cache_clear()
    try:
        database.engine()
        assert create.call_args.kwargs == {
            "pool_pre_ping": True,
            "connect_args": {"sslmode": "verify-full", "sslrootcert": str(ca)},
        }
        for url in [settings.database_url + "?sslmode=disable", settings.database_url]:
            database.engine.cache_clear()
            settings.database_url = url
            if "disable" not in url:
                ca.unlink()
            with pytest.raises(ValueError):
                database.engine()
    finally:
        database.engine.cache_clear()


def test_pinned_schema_cache_rejects_missing_or_modified_inputs_without_network(tmp_path):
    from platform_checks import load_schemas

    with pytest.raises(ValueError, match="Missing or changed schema"):
        load_schemas(cache=tmp_path)
    (tmp_path / "cnpg-cluster-1.30.1.yaml").write_text("modified upstream content")
    with pytest.raises(ValueError, match="Missing or changed schema"):
        load_schemas(cache=tmp_path)


def test_unknown_custom_resource_fields_cannot_be_silently_pruned():
    import jsonschema
    from platform_checks import strict_properties

    schema = {
        "type": "object",
        "properties": {
            "policy": {"type": "object", "properties": {"durable": {"type": "boolean"}}},
            "labels": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    }
    validator = jsonschema.Draft7Validator(strict_properties(schema))
    validator.validate({"policy": {"durable": True}, "labels": {"arbitrary": "value"}})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"policy": {"misspelledDurability": True}})


def test_database_web_identity_has_no_static_credentials_and_is_namespace_scoped():
    resources = render(
        "klip-database",
        [
            "backup.authentication=webIdentity",
            "backup.credentialsSecret=",
            "backup.roleArn=arn:aws:iam::127811062476:role/fixture-backup",
            "networkPolicy.managementNamespace=management-fixture",
        ],
    )
    db = next(x for x in resources if x["kind"] == "Cluster")
    assert (
        db["spec"]["serviceAccountTemplate"]["metadata"]["annotations"][
            "eks.amazonaws.com/role-arn"
        ]
        == "arn:aws:iam::127811062476:role/fixture-backup"
    )
    store = next(x for x in resources if x["kind"] == "ObjectStore")
    assert store["spec"]["configuration"]["s3Credentials"] == {"inheritFromIAMRole": True}
    policy = next(x for x in resources if x["kind"] == "NetworkPolicy")
    clients = policy["spec"]["ingress"][1]["from"]
    assert len(clients) == 1
    assert clients[0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "management-fixture"
    }
    assert "podSelector" in clients[0]  # AND, not an unrestricted namespace OR.
    assert "webIdentity requires" in render(
        "klip-database",
        [
            "backup.authentication=webIdentity",
            "backup.roleArn=arn:aws:iam::127811062476:role/fixture-backup",
        ],
        success=False,
    )
