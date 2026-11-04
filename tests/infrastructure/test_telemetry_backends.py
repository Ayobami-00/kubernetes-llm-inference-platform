"""Private telemetry deployment, credential custody and native-sidecar resource accounting."""

import base64
import copy
import json

import grafana_storage
import pytest
import telemetry_secrets
from common import CONFIG
from observability_release import backend_values
from telemetry_budget import quantity, requests


def test_cnpg_omitted_false_privileges_do_not_change_owned_role_semantics():
    import yaml
    from common import ROOT

    role = next(
        yaml.safe_load_all((ROOT / "deploy/observability/grafana-database.yaml").read_text())
    )
    actual = copy.deepcopy(role)
    privileges = ("superuser", "createdb", "createrole", "replication", "bypassrls")
    for field in privileges:
        del actual["spec"][field]
    assert grafana_storage.owned_spec_matches(role, actual)
    for field in privileges:
        changed = copy.deepcopy(actual)
        changed["spec"][field] = True
        assert not grafana_storage.owned_spec_matches(role, changed)
    for field, value in (("ensure", "absent"), ("connectionLimit", 100)):
        changed = copy.deepcopy(actual)
        changed["spec"][field] = value
        assert not grafana_storage.owned_spec_matches(role, changed)
    actual["metadata"]["labels"].clear()
    assert not grafana_storage.owned_spec_matches(role, actual)


def test_grafana_uses_shared_postgres_and_retained_encryption_key_without_api_access():
    v = backend_values("grafana")
    assert v["replicas"] == 2 and not v["persistence"]["enabled"]
    assert not v["automountServiceAccountToken"] and not v["rbac"]["create"]
    assert v["grafana.ini"]["database"]["ssl_mode"] == "verify-full"
    assert v["grafana.ini"]["database"]["name"] == "grafana"
    assert v["grafana.ini"]["security"]["secret_key"].startswith("$__file{")
    assert not v["grafana.ini"]["auth.anonymous"]["enabled"]
    assert not v["grafana.ini"]["unified_alerting"]["enabled"]
    assert "cloudwatch" in v["grafana.ini"]["plugins"]["disable_plugins"]
    assert len(v["datasources"]["datasources.yaml"]["datasources"]) == 3
    import yaml

    dashboards = yaml.safe_load(v["extraObjects"][0])
    assert dashboards["kind"] == "ConfigMap" and len(dashboards["data"]) == 6
    assert v["dashboardsConfigMaps"] == {"klip": "klip-grafana-dashboards"}
    assert not v.get("dashboards")  # The chart otherwise launches a download container.


def test_loki_is_monolithic_and_collector_queues_survive_pod_replacement():
    loki = backend_values("loki")
    assert loki["deploymentMode"] == "Monolithic" and loki["singleBinary"]["replicas"] == 3
    assert loki["loki"]["commonConfig"]["replication_factor"] == 3
    assert loki["loki"]["limits_config"]["retention_period"] == "720h"
    assert not loki["gateway"]["enabled"] and not loki["lokiCanary"]["enabled"]
    assert not loki["serviceAccount"]["automountServiceAccountToken"]
    collector = backend_values("otel")
    assert collector["mode"] == "statefulset" and collector["replicaCount"] == 2
    for exporter in ("otlphttp/logs", "otlp/traces"):
        assert (
            collector["config"]["exporters"][exporter]["sending_queue"]["storage"] == "file_storage"
        )
    assert collector["config"]["exporters"]["debug"] is None
    assert backend_values("tempo")["replicas"] == 1
    assert backend_values("tempo")["tempo"]["retention"] == "168h"


def test_credentials_never_replace_a_live_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(grafana_storage, "state_home", lambda: tmp_path)
    monkeypatch.setattr(grafana_storage, "secret_get", lambda *_: {"synthetic": True})
    with pytest.raises(ValueError, match="Recover"):
        grafana_storage.credentials()
    assert not (tmp_path / "credentials/grafana.json").exists()
    monkeypatch.setattr(grafana_storage, "secret_get", lambda *_: None)
    first = grafana_storage.credentials()
    assert grafana_storage.credentials() == first
    monkeypatch.setattr(
        grafana_storage,
        "secret_get",
        lambda *_: {"type": "Opaque", "data": {"key": base64.b64encode(b"synthetic-old").decode()}},
    )
    with pytest.raises(ValueError, match="differ"):
        grafana_storage.ensure_secret("synthetic", "observability", {"key": "synthetic-new"})


def test_recovery_payload_is_bound_to_project_and_all_required_secrets():
    payload = {
        "schema_version": "klip-grafana-secret.v1",
        "account_id": CONFIG["account_id"],
        "cluster_id": CONFIG["cluster_id"],
        "credential": {
            "admin-user": "admin",
            **{
                k: "synthetic-" + "x" * 48
                for k in ("admin-password", "database-password", "encryption-key")
            },
        },
    }
    assert telemetry_secrets.validate(payload) == payload["credential"]
    changed = copy.deepcopy(payload)
    changed["cluster_id"] = "another-cluster"
    with pytest.raises(ValueError):
        telemetry_secrets.validate(changed)
    changed = copy.deepcopy(payload)
    changed["credential"].pop("encryption-key")
    with pytest.raises(ValueError):
        telemetry_secrets.validate(changed)
    assert "synthetic" in json.dumps(payload)


def test_native_sidecars_and_init_peak_are_included_in_cpu_and_memory():
    def container(cpu, memory, **kwargs):
        return {"resources": {"requests": {"cpu": cpu, "memory": memory}}, **kwargs}

    pod = {
        "containers": [container("200m", "400Mi")],
        "initContainers": [
            container("50m", "64Mi", restartPolicy="Always"),
            container("1", "128Mi"),
        ],
    }
    assert requests(pod) == [1.05, 464 * 1024**2]
    assert quantity("1Gi", "memory") == 1024**3
    with pytest.raises(ValueError):
        quantity("NaN", "cpu")
