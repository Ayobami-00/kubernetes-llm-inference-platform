"""Monitoring renders fail closed on mutable runtimes and privileged networking."""

import hashlib
import io
import json
import subprocess
import tarfile

import observability_release as release
import pytest
import yaml
from common import tool


def test_upstream_operator_enum_uses_safe_yaml_scalar_compatibility():
    value = yaml.load("enum:\n- =\n- =~\n- '!='\n", Loader=release.CRDLoader)
    assert value == {"enum": ["=", "=~", "!="]}
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load("!!python/object/apply:builtins.print ['unsafe']", Loader=release.CRDLoader)


def test_monitoring_uses_rotating_credentials_and_separate_scoped_roles():
    values = release.monitoring_values()
    assert not values["prometheus"]["serviceAccount"]["createTokenSecret"]
    assert values["prometheus"]["controlPlaneScrapeClass"]["enabled"]
    prom = values["prometheus"]["prometheusSpec"]
    assert prom["replicas"] == 2 and prom["podAntiAffinity"] == "hard"
    assert prom["replicaExternalLabelName"] == "prometheus_replica"
    assert (
        not prom["enableAdminAPI"]
        and prom["thanos"]["objectStorageConfig"]["secret"]["config"]["aws_sdk_auth"]
    )
    assert values["alertmanager"]["alertmanagerSpec"]["replicas"] == 3
    notifications = values["alertmanager"]["config"]["receivers"][1]["sns_configs"][0]
    assert notifications["sigv4"] == {"region": "us-east-1"}
    assert notifications["send_resolved"]
    assert "access_key" not in json.dumps(values) and "secret_key" not in json.dumps(values)
    assert (
        values["prometheus"]["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"]
        != values["alertmanager"]["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"]
    )
    assert not values["prometheus-node-exporter"]["hostNetwork"]
    assert not values["prometheus-node-exporter"]["hostPID"]
    assert values["kube-state-metrics"]["collectorsExclude"] == ["secrets", "configmaps"]
    assert values["prometheusOperator"]["namespaces"] == {
        "releaseNamespace": True,
        "additional": ["observability-node-agents"],
    }
    assert prom["arbitraryFSAccessThroughSMs"] == {"deny": True}
    assert not values["kubelet"]["serviceMonitor"]["tlsConfig"]["insecureSkipVerify"]
    assert (
        values["prometheusOperator"]["admissionWebhooks"]["failurePolicy"] == "IgnoreOnInstallOnly"
    )
    assert all(
        c["env"] == [{"name": "AWS_EC2_METADATA_DISABLED", "value": "true"}]
        for c in prom["containers"]
    )


def test_workload_labels_survive_target_collision_without_honoring_untrusted_labels():
    import re

    values = release.monitoring_values()
    assert values["prometheus"]["prometheusSpec"]["overrideHonorLabels"] is True
    labels = {
        "namespace": "observability",
        "pod": "ksm-exporter",
        "container": "kube-state-metrics",
        "exported_namespace": "platform-data",
        "exported_pod": "klip-db-1",
        "exported_container": "postgres",
        "job": "kube-state-metrics",
        "cluster": "klip",
    }
    for rule in values["kube-state-metrics"]["prometheus"]["monitor"]["metricRelabelings"]:
        if rule["action"] == "labeldrop":
            labels = {k: v for k, v in labels.items() if not re.fullmatch(rule["regex"], k)}
        else:
            value = ";".join(labels.get(k, "") for k in rule["sourceLabels"])
            if re.fullmatch(rule["regex"], value):
                labels[rule["targetLabel"]] = value
    assert labels == {
        "namespace": "platform-data",
        "pod": "klip-db-1",
        "container": "postgres",
        "scrape_namespace": "observability",
        "scrape_pod": "ksm-exporter",
        "scrape_container": "kube-state-metrics",
        "job": "kube-state-metrics",
        "cluster": "klip",
    }
    path = release.CACHE / "kube-prometheus-stack/values.yaml"
    if path.exists():
        upstream = yaml.safe_load(path.read_text())["kubelet"]["serviceMonitor"]
        configured = values["kubelet"]["serviceMonitor"]
        for field in ("metricRelabelings", "cAdvisorMetricRelabelings", "probesMetricRelabelings"):
            assert configured[field] == upstream[field] + release.workload_label_relabelings(
                ["namespace"]
            )


@pytest.mark.skipif(
    not (release.CACHE / "kube-prometheus-stack/Chart.yaml").exists(),
    reason="Requires the checksum-verified upstream monitoring chart",
)
def test_upstream_admission_is_ignore_only_during_first_install():
    for upgrading, expected in [(False, "Ignore"), (True, "Fail")]:
        command = [
            tool("helm"),
            "template",
            "klip-monitoring",
            str(release.CACHE / "kube-prometheus-stack"),
            "--namespace",
            "observability",
            "--kube-version",
            "1.36.5",
            "--values",
            "-",
        ]
        if upgrading:
            command.append("--is-upgrade")
        output = subprocess.run(
            command,
            input=yaml.safe_dump(release.monitoring_values()),
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        policies = [
            hook["failurePolicy"]
            for doc in yaml.safe_load_all(output)
            if doc
            and doc["kind"] in {"MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}
            for hook in doc["webhooks"]
        ]
        assert len(policies) == 3 and set(policies) == {expected}


@pytest.mark.parametrize(
    "document",
    [
        {"kind": "Secret", "type": "kubernetes.io/service-account-token"},
        {"kind": "Deployment", "spec": {"template": {"spec": {"hostNetwork": True}}}},
        {"kind": "Deployment", "spec": {"template": {"spec": {"nodeSelector": {}}}}},
        {"kind": "ConfigMap", "data": {"image": "quay.io/thanos/thanos:latest"}},
        {"kind": "Deployment", "spec": {"args": ["--prometheus-config-reloader=example:latest"]}},
        {"kind": "Prometheus", "spec": {"replicas": 1, "nodeSelector": release.PLACEMENT}},
        {
            "kind": "ClusterRole",
            "metadata": {"name": "klip-monitoring-kube-state-metrics"},
            "rules": [{"resources": ["secrets"], "verbs": ["list", "watch"]}],
        },
        {
            "kind": "DaemonSet",
            "spec": {"template": {"spec": {"automountServiceAccountToken": True}}},
        },
    ],
)
def test_monitoring_rejects_unsafe_rendered_defaults(document):
    with pytest.raises(ValueError):
        release.check_render([document])


def archive_fixture(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(release, "CACHE", cache)
    archive = cache / "tempo-3.2.1.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        content = b"apiVersion: v2\nname: tempo\nversion: 3.2.1\n"
        item = tarfile.TarInfo("tempo/Chart.yaml")
        item.size = len(content)
        tar.addfile(item, io.BytesIO(content))
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "charts": {
                    "tempo": {
                        "version": "3.2.1",
                        "file": archive.name,
                        "url": "https://github.com/grafana-community/helm-charts/releases/download/tempo-3.2.1/tempo-3.2.1.tgz",
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    }
                }
            }
        )
    )
    monkeypatch.setattr(release, "LOCK", lock)
    return cache, archive


def test_verified_reextraction_removes_stale_templates(tmp_path, monkeypatch):
    cache, _ = archive_fixture(tmp_path, monkeypatch)
    (cache / "tempo").mkdir()
    (cache / "tempo" / "unexpected-template.yaml").write_text("stale")
    release.fetch()
    assert (cache / "tempo" / "Chart.yaml").exists()
    assert not (cache / "tempo" / "unexpected-template.yaml").exists()


def test_corrupted_cached_chart_is_never_used(tmp_path, monkeypatch):
    _, archive = archive_fixture(tmp_path, monkeypatch)
    archive.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        release.fetch()
