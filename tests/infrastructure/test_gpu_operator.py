"""GPU ownership, immutable artifacts and narrow initial-install boundaries."""

import copy
import json

import gpu_operator as gpu
import pytest
import yaml


def cpu_cluster(*args):
    if args == ("get", "nodes", "-o", "json"):
        return json.dumps(
            {
                "items": [
                    {"metadata": {"name": name, "uid": name + "-uid", "labels": {}}, "status": {}}
                    for name in (
                        "cp-01",
                        "cp-02",
                        "cp-03",
                        "platform-01",
                        "platform-02",
                        "platform-03",
                    )
                ]
            }
        )
    if args == ("get", "deployments,daemonsets", "-A", "-o", "json"):
        return '{"items": []}'
    if args == ("get", "namespace", "kube-system", "-o", "json"):
        return '{"metadata": {"uid": "cluster-uid"}}'
    if "--ignore-not-found" in args:
        return ""
    raise AssertionError(args)


def test_operand_versions_are_immutable_and_driver_includes_its_os():
    values = gpu.values()
    for key in (
        "operator",
        "validator",
        "driver",
        "toolkit",
        "devicePlugin",
        "gfd",
        "dcgmExporter",
    ):
        assert "@sha256:" in values[key]["version"]
    assert values["driver"]["version"].startswith("595.91.07-ubuntu24.04@sha256:")
    assert values["driver"]["manager"]["version"].startswith("v0.12.1@sha256:")
    assert values["driver"]["upgradePolicy"]["autoUpgrade"] is False
    assert values["mig"]["strategy"] == "none"
    assert all(
        values[key]["enabled"] is False
        for key in (
            "migManager",
            "dcgm",
            "gds",
            "gdrcopy",
            "vgpuManager",
            "vfioManager",
            "ccManager",
            "sandboxDevicePlugin",
            "kataSandboxDevicePlugin",
            "nodeStatusExporter",
        )
    )


def test_feature_discovery_cannot_bootstrap_itself_on_cpu_or_unadmitted_hosts():
    values = gpu.values()
    nfd = gpu.nfd_values()
    assert values["nfd"]["enabled"] is False
    assert values["operator"]["nodeSelector"] == gpu.PLACEMENT
    assert nfd["master"]["nodeSelector"] == nfd["gc"]["nodeSelector"] == gpu.PLACEMENT
    assert nfd["worker"]["nodeSelector"] == gpu.CANDIDATE
    assert nfd["postDeleteCleanup"] is False
    assert gpu.BOOTSTRAP in nfd["worker"]["tolerations"]
    assert gpu.BOOTSTRAP in values["daemonsets"]["tolerations"]
    assert values["operator"]["tolerations"] == []


def test_privilege_exception_and_metric_access_are_confined_to_gpu_namespace():
    namespace, policy, driver, prometheus = yaml.safe_load_all(gpu.boundary())
    assert namespace["metadata"]["name"] == policy["metadata"]["namespace"] == "gpu-system"
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "privileged"
    metrics = policy["spec"]["ingress"][1]
    assert metrics["fromEndpoints"][0]["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": "observability",
        "app.kubernetes.io/name": "prometheus",
    }
    assert metrics["toPorts"][0]["ports"] == [{"port": "9400", "protocol": "TCP"}]
    assert not any("toCIDR" in rule for rule in policy["spec"]["egress"])
    assert not any("world" in rule.get("toEntities", []) for rule in policy["spec"]["egress"])
    assert driver["spec"]["endpointSelector"] == {"matchLabels": {"app": "nvidia-driver-daemonset"}}
    assert prometheus["metadata"]["namespace"] == "observability"
    assert prometheus["spec"]["endpointSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": "prometheus"}
    }
    assert prometheus["spec"]["egress"][0]["toPorts"] == [
        {"ports": [{"port": "9400", "protocol": "TCP"}]}
    ]
    assert gpu.values()["dcgmExporter"]["serviceMonitor"]["additionalLabels"] == {
        "release": "klip-monitoring"
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://helm.ngc.nvidia.com/chart",
        "https://example.com/chart",
        "https://user@xfiles.ngc.nvidia.com/chart",
        "https://xfiles.ngc.nvidia.com:8443/chart",
    ],
)
def test_chart_redirect_never_reaches_unreviewed_host_or_credentials(url):
    with pytest.raises(ValueError, match="reviewed public hosts"):
        gpu.ChartRedirect().redirect_request(None, None, 302, "", {}, url)


def test_initial_install_records_all_cpu_uids_and_cluster_uid():
    observed = gpu.observe_initial(cpu_cluster)
    assert len(observed["nodes"]) == 6
    assert observed["kube_system_uid"] == "cluster-uid"


@pytest.mark.parametrize(
    "conflict", ["candidate", "capacity", "feature", "foreign-host", "nfd", "crd"]
)
def test_initial_install_refuses_existing_gpu_ownership_or_wrong_hosts(conflict):
    def cluster(*args):
        result = cpu_cluster(*args)
        if args[:2] == ("get", "nodes"):
            document = json.loads(result)
            node = document["items"][0]
            if conflict == "candidate":
                node["metadata"]["labels"] = gpu.CANDIDATE
            elif conflict == "capacity":
                node["status"] = {"capacity": {"nvidia.com/gpu": "1"}}
            elif conflict == "feature":
                node["metadata"]["labels"] = {"feature.node.kubernetes.io/pci-10de.present": "true"}
            elif conflict == "foreign-host":
                node["metadata"]["name"] = "foreign"
            return json.dumps(document)
        if conflict == "nfd" and args[:2] == ("get", "deployments,daemonsets"):
            return '{"items": [{"metadata": {"name": "existing-node-feature-discovery"}}]}'
        if conflict == "crd" and args[:2] == ("get", "crd"):
            return "customresourcedefinition.apiextensions.k8s.io/clusterpolicies.nvidia.com"
        return result

    with pytest.raises(ValueError):
        gpu.observe_initial(cluster)


def test_saved_plan_rejects_changed_source_and_replay_before_cluster_access(monkeypatch, tmp_path):
    import cluster
    import gpu_operator_security

    monkeypatch.setattr(gpu, "folder", lambda: tmp_path)
    monkeypatch.setattr(gpu, "fetch", lambda: None)
    monkeypatch.setattr(gpu, "render", lambda: "frozen rendered content")
    monkeypatch.setattr(gpu, "source_digest", lambda: "original")
    monkeypatch.setattr(cluster, "kubectl", cpu_cluster)
    monkeypatch.setattr(gpu_operator_security, "assess", lambda path: {"review": "test"})
    plan = gpu.prepare(tmp_path)
    monkeypatch.setattr(cluster, "kubectl", lambda *a: pytest.fail("Must reject before cluster IO"))
    monkeypatch.setattr(gpu, "source_digest", lambda: "changed")
    with pytest.raises(ValueError, match="plan changed"):
        gpu.apply(plan["approval_sha256"])
    monkeypatch.setattr(gpu, "source_digest", lambda: "original")
    (tmp_path / "plans" / plan["approval_sha256"] / "result.json").write_text(
        '{"status":"running"}'
    )
    with pytest.raises(ValueError, match="already executed"):
        gpu.apply(plan["approval_sha256"])


@pytest.mark.skipif(
    not (gpu.CACHE / gpu.RELEASE / "Chart.yaml").exists(), reason="Verified chart cache required"
)
def test_actual_upstream_chart_has_only_locked_components_and_candidate_workers():
    documents = gpu.validate_render(gpu.render())
    policy = next(d["spec"] for d in documents if d["kind"] == "ClusterPolicy")
    assert gpu.BOOTSTRAP in policy["daemonsets"]["tolerations"]
    assert all(
        not policy[key].get("enabled", False)
        for key in (
            "migManager",
            "gds",
            "gdrcopy",
            "vfioManager",
            "vgpuDeviceManager",
            "ccManager",
            "sandboxDevicePlugin",
            "kataSandboxDevicePlugin",
        )
    )
    changed = copy.deepcopy(documents)
    deployment = next(d for d in changed if d["kind"] == "Deployment")
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
        "nvcr.io/nvidia/gpu-operator:latest"
    )
    with pytest.raises(ValueError, match="image is not locked"):
        gpu.validate_render(yaml.safe_dump_all(changed))
