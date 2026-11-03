"""Monitoring renders fail closed on mutable runtimes and privileged networking."""

import hashlib
import io
import json
import tarfile

import observability_release as release
import pytest
import yaml


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
    assert all(
        c["env"] == [{"name": "AWS_EC2_METADATA_DISABLED", "value": "true"}]
        for c in prom["containers"]
    )


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
