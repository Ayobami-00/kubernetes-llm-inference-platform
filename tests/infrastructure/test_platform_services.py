"""Validate actual upstream Helm rendering and the limited credential injection path."""

import json

import pytest
import yaml
from common import CONFIG, ROOT
from platform_services import CACHE, RELEASES, render, values_for

pytestmark = pytest.mark.skipif(
    not (CACHE / "cert-manager/Chart.yaml").is_file(),
    reason="Requires checksum-verified platform service chart cache",
)


def resources(release):
    storage = {
        "artifacts": {
            "node_role_arn": (
                f"arn:aws:iam::{CONFIG['account_id']}:role/"
                f"{CONFIG['project']}/klip-storage-artifacts"
            ),
            "file_system_id": "fs-00000000000000001",
            "access_point_id": "fsap-00000000000000001",
        }
    }
    storage["ebs_controller_role_arn"] = (
        f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-ebs-controller"
    )
    return [
        x for x in yaml.safe_load_all(render(release, values_for(release, storage=storage))) if x
    ]


def test_certificate_services_are_spread_and_all_images_are_immutable():
    items = resources("cert-manager")
    for item in items:
        if item["kind"] != "Deployment":
            continue
        spec = item["spec"]
        pod = spec["template"]["spec"]
        assert spec["replicas"] == 2
        assert pod["nodeSelector"]["klip.dev.node-restriction.kubernetes.io/platform"] == "true"
        assert pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        assert all("@sha256:" in container["image"] for container in pod["containers"])
        for container in pod["containers"]:
            for arg in container.get("args", []):
                if arg.startswith("--acme-http01-solver-image="):
                    assert "@sha256:" in arg
    assert len([x for x in items if x["kind"] == "PodDisruptionBudget"]) == 3


def test_webhook_only_reads_accounts_and_has_fail_closed_exact_account_scope():
    items = resources("klip-workload-identity")
    role = next(x for x in items if x["kind"] == "ClusterRole")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["get", "list", "watch"]}
    ]
    hook = next(x for x in items if x["kind"] == "MutatingWebhookConfiguration")["webhooks"][0]
    assert hook["failurePolicy"] == "Fail"
    expression = hook["matchConditions"][0]["expression"]
    assert "has(object.spec.serviceAccountName)" in expression
    assert "request.namespace == 'kube-system'" in expression
    assert "request.namespace == 'platform-data'" in expression
    assert "request.namespace == 'observability'" in expression
    for account in (
        "klip-prometheus",
        "klip-thanos-store",
        "klip-thanos-compactor",
        "klip-loki",
        "klip-tempo",
        "klip-alertmanager",
    ):
        assert "'" + account + "'" in expression
    assert "ebs-csi-controller-sa" in expression and "efs-csi-node-sa" in expression
    pod = next(x for x in items if x["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"]
    assert "--in-cluster=false" in pod["containers"][0]["command"]


def test_telemetry_boundary_separates_host_mounts_and_has_no_public_service():
    documents = resources("klip-observability")
    namespaces = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Namespace"}
    key = "pod-security.kubernetes.io/enforce"
    assert namespaces["observability"]["metadata"]["labels"][key] == "restricted"
    assert namespaces["observability-node-agents"]["metadata"]["labels"][key] == "privileged"
    deny = [d for d in documents if d["kind"] == "NetworkPolicy"]
    assert len(deny) == 2
    assert all(d["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]} for d in deny)
    host = next(d for d in documents if d["metadata"]["name"] == "host-metrics")
    assert "egress" not in host["spec"]
    assert host["spec"]["ingress"][0]["fromEndpoints"][0]["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": "observability",
        "app.kubernetes.io/name": "prometheus",
    }
    assert not any(d["kind"] in {"Service", "Ingress", "Gateway", "Deployment"} for d in documents)


def test_static_efs_driver_cannot_mutate_nodes_or_create_access_points():
    items = resources("aws-efs-csi-driver")
    assert not [x for x in items if x["kind"] == "Deployment"]
    node = next(x for x in items if x["kind"] == "DaemonSet")
    pod = node["spec"]["template"]["spec"]
    assert node["spec"]["updateStrategy"]["rollingUpdate"]["maxUnavailable"] == 1
    assert pod["nodeSelector"]["klip.dev.node-restriction.kubernetes.io/platform"] == "true"
    assert pod["hostNetwork"] and pod["dnsPolicy"] == "Default"
    plugin = next(x for x in pod["containers"] if x["name"] == "efs-plugin")
    assert {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"} in plugin["env"]
    assert all("@sha256:" in x["image"] for x in pod["containers"])
    for item in items:
        if item["kind"] in {"Role", "ClusterRole"}:
            assert all(
                not set(x["verbs"]) & {"create", "update", "patch", "delete", "*"}
                for x in item["rules"]
            )


def test_nonreviewed_roles_and_releases_fail_before_deployment():
    with pytest.raises(ValueError, match="Unreviewed"):
        values_for("unknown")
    with pytest.raises(ValueError, match="Unexpected artifact"):
        values_for(
            "aws-efs-csi-driver",
            storage={"artifacts": {"node_role_arn": "arn:aws:iam::999999999999:role/admin"}},
        )
    pins = json.loads((ROOT / "deploy/platform-services.lock.json").read_text())
    assert set(RELEASES) == {
        "cert-manager",
        "klip-workload-identity",
        "aws-efs-csi-driver",
        "aws-ebs-csi-driver",
        "klip-storage",
        "cloudnative-pg",
        "plugin-barman-cloud",
        "klip-image-pull",
        "klip-observability",
    }
    assert len(pins["images"]) >= 9


def test_artifact_volume_is_static_retained_non_bypassable_and_bound_to_one_claim():
    items = resources("klip-storage")
    pv = next(x for x in items if x["kind"] == "PersistentVolume")["spec"]
    pvc = next(x for x in items if x["kind"] == "PersistentVolumeClaim")["spec"]
    assert pv["persistentVolumeReclaimPolicy"] == "Retain"
    assert pv["mountOptions"] == ["tls", "iam"]
    assert pv["csi"]["volumeHandle"] == "fs-00000000000000001::fsap-00000000000000001"
    assert pv["claimRef"] == {"name": "klip-artifacts", "namespace": "klip"}
    assert pvc["volumeName"] == "klip-artifacts" and pvc["storageClassName"] == ""
    assert (
        pv["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]["key"]
        == "klip.dev.node-restriction.kubernetes.io/platform"
    )


def test_storage_probe_uses_the_real_claim_without_app_or_aws_credentials():
    from storage_acceptance import job_document

    doc = job_document("0123456789ab", "platform-01", "write")
    spec = doc["spec"]["template"]["spec"]
    assert not spec["automountServiceAccountToken"]
    assert spec["securityContext"]["runAsUser"] == 10001
    assert spec["volumes"] == [
        {"name": "artifacts", "persistentVolumeClaim": {"claimName": "klip-artifacts"}}
    ]
    assert {v["name"] for v in spec["containers"][0]["env"]} == {
        "KLIP_OPERATION",
        "KLIP_NODE",
        "KLIP_STAGE",
        "AWS_EC2_METADATA_DISABLED",
    }
    with pytest.raises(ValueError):
        job_document("../../other-data", "platform-01", "cleanup")


def test_ebs_is_topology_aware_retained_and_has_no_static_or_metadata_credentials():
    items = resources("aws-ebs-csi-driver")
    storage = next(x for x in items if x["kind"] == "StorageClass")
    assert storage["metadata"]["name"] == "klip-gp3"
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage["reclaimPolicy"] == "Retain" and storage["allowVolumeExpansion"]
    assert storage["parameters"]["encrypted"] == "true"
    controller = next(x for x in items if x["kind"] == "Deployment")
    assert controller["spec"]["replicas"] == 2
    assert controller["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    node = next(x for x in items if x["kind"] == "DaemonSet")
    assert node["spec"]["template"]["spec"]["serviceAccountName"] == "ebs-csi-node-sa"
    for item in (controller, node):
        spec = item["spec"]["template"]["spec"]
        assert spec["nodeSelector"]["klip.dev.node-restriction.kubernetes.io/platform"] == "true"
        for container in spec["containers"]:
            assert "@sha256:" in container["image"]
            names = {e["name"] for e in container.get("env", [])}
            assert not names & {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
        plugin = next(c for c in spec["containers"] if c["name"] == "ebs-plugin")
        assert {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"} in plugin["env"]
    assert (
        "--metadata-sources=kubernetes" in node["spec"]["template"]["spec"]["containers"][0]["args"]
    )
    assert not [x for x in items if x["kind"] == "DaemonSet" and "windows" in x["metadata"]["name"]]


def test_database_operators_use_pinned_images_and_truthful_replica_modes():
    for release, replicas in [("cloudnative-pg", 2), ("plugin-barman-cloud", 1)]:
        items = resources(release)
        deployment = next(x for x in items if x["kind"] == "Deployment")
        assert deployment["spec"]["replicas"] == replicas
        pod = deployment["spec"]["template"]["spec"]
        assert pod["nodeSelector"]["klip.dev.node-restriction.kubernetes.io/platform"] == "true"
        assert all("@sha256:" in x["image"] for x in pod["containers"])
        if release == "plugin-barman-cloud":
            assert deployment["spec"]["strategy"]["type"] == "Recreate"
            config = next(x for x in items if x["kind"] == "ConfigMap")
            assert "@sha256:" in config["data"]["SIDECAR_IMAGE"]
