"""A live CPU maintenance plan must remain one drained, unchanged host identity."""

import copy
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import cluster
import manage
import platform_frontend as frontend
import platform_resize as resize
import pytest


def plan(name="platform-01"):
    before = {
        "id": "i-owned",
        "instance_type": "m7i.xlarge",
        "ami": "ami-pinned",
        "root_block_device": [{"volume_id": "vol-retained"}],
        "cpu_options": [{"core_count": 2, "threads_per_core": 2}],
    }
    after = {
        **copy.deepcopy(before),
        "instance_type": "m7i.2xlarge",
        "cpu_options": [{"core_count": 4, "threads_per_core": 2}],
    }
    return {
        "resource_changes": [
            {
                "address": f'module.compute.aws_instance.node["{name}"]',
                "type": "aws_instance",
                "change": {"actions": ["update"], "before": before, "after": after},
            }
        ]
    }


def test_only_one_size_change_with_original_disk_and_full_cpus_is_accepted():
    document = plan()
    assert resize.resize_change(document, "platform-01") == document["resource_changes"][0]
    duplicate = copy.deepcopy(document)
    duplicate["resource_changes"] += plan("platform-02")["resource_changes"]
    with pytest.raises(ValueError, match="Exactly one"):
        resize.resize_change(duplicate)
    for key, value in [
        ("ami", "different"),
        ("root_block_device", [{"volume_id": "new"}]),
        ("cpu_options", [{"core_count": 2, "threads_per_core": 2}]),
    ]:
        altered = copy.deepcopy(document)
        altered["resource_changes"][0]["change"]["after"][key] = value
        with pytest.raises(ValueError):
            resize.resize_change(altered)
    for change in (
        plan("cp-01"),
        {"resource_changes": [{"type": "aws_s3_bucket", "change": {"actions": ["create"]}}]},
    ):
        with pytest.raises(ValueError):
            resize.resize_change(change)


def test_public_addresses_may_only_be_recomputed_not_changed():
    value = plan()
    c = value["resource_changes"][0]["change"]
    c["before"]["public_ip"] = "192.0.2.1"
    c["after"]["public_ip"] = None
    with pytest.raises(ValueError):
        resize.resize_change(value)
    c["after_unknown"] = {"public_ip": True}
    resize.resize_change(value)
    c["after"]["public_ip"] = "192.0.2.2"
    c["after_unknown"] = {}
    with pytest.raises(ValueError):
        resize.resize_change(value)


def test_generic_apply_requires_fresh_matching_drain_and_live_survivors(monkeypatch):
    record = {
        "plan_sha256": "sha",
        "stage": "applying",
        "instance_id": "i-owned",
        "node": "platform-01",
        "drained_at": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(resize, "active", lambda: record)
    monkeypatch.setattr(resize, "node", lambda _: {"spec": {"unschedulable": True}})
    monkeypatch.setattr(resize, "workload_pods", lambda _: [])
    monkeypatch.setattr(resize, "no_active_jobs", Mock())
    monkeypatch.setattr(frontend, "require_detached", Mock())
    survivors = Mock()
    monkeypatch.setattr(resize, "assert_survivors", survivors)
    resize.verify_for_apply({"plan_sha256": "sha"}, plan())
    survivors.assert_called_once_with(record)
    record["drained_at"] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    with pytest.raises(ValueError, match="expired"):
        resize.verify_for_apply({"plan_sha256": "sha"}, plan())
    record["drained_at"] = datetime.now(UTC).isoformat()
    record["stage"] = "prepared"
    with pytest.raises(ValueError, match="drained"):
        resize.verify_for_apply({"plan_sha256": "sha"}, plan())


def test_terminal_reuse_and_interrupted_apply_never_replay_terraform(monkeypatch):
    record = {"plan_sha256": "sha", "stage": "completed"}
    monkeypatch.setattr(resize, "active", lambda: record)
    mutation = Mock()
    monkeypatch.setattr(manage, "apply_plan", mutation)
    assert resize.apply("sha") == record
    recovery = Mock(return_value={"stage": "completed"})
    monkeypatch.setattr(resize, "recover", recovery)
    record["stage"] = "applying"
    resize.apply("sha")
    recovery.assert_called_once_with(record)
    mutation.assert_not_called()
    with pytest.raises(ValueError, match="exact"):
        resize.apply("conflict")


def test_node_patch_is_uid_and_resource_version_bound(monkeypatch):
    value = {
        "metadata": {"uid": "uid", "resourceVersion": "5", "labels": {resize.PROTECTED: "true"}},
        "spec": {},
    }
    monkeypatch.setattr(resize, "node", lambda _: value)
    patcher = Mock()
    monkeypatch.setattr(cluster, "kubectl", patcher)
    record = {
        "node": "platform-01",
        "node_uids": {"platform-01": "uid"},
        "desired_type": "m7i.2xlarge",
    }
    resize.set_cordon(record, False)
    patch = json.loads(patcher.call_args.args[-1])
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "5"},
    ]
    assert patch[-1]["value"] == "m7i.2xlarge"
    value["metadata"]["uid"] = "replacement"
    with pytest.raises(ValueError, match="replacement"):
        resize.set_cordon(record, True)


def test_real_exposed_cpus_and_allocatable_memory_are_required():
    value = {
        "status": {"capacity": {"cpu": "8"}, "allocatable": {"cpu": "7500m", "memory": "30Gi"}}
    }
    assert resize.sufficient_capacity(value)
    value["status"]["capacity"]["cpu"] = "4"
    assert not resize.sufficient_capacity(value)
    value["status"]["capacity"]["cpu"] = "8"
    value["status"]["allocatable"]["memory"] = "16Gi"
    assert not resize.sufficient_capacity(value)


def test_abort_never_undoes_an_ambiguous_aws_transition(monkeypatch):
    record = {"plan_sha256": "sha", "stage": "applying"}
    monkeypatch.setattr(resize, "active", lambda: record)
    aws = Mock()
    monkeypatch.setattr(resize, "Aws", aws)
    with pytest.raises(ValueError, match="before any Terraform"):
        resize.abort_drain("sha")
    aws.assert_not_called()


def test_active_job_on_another_host_prevents_maintenance(monkeypatch):
    job = {
        "metadata": {"ownerReferences": [{"kind": "Job", "controller": True}]},
        "status": {"phase": "Running"},
    }
    monkeypatch.setattr(cluster, "kubectl", Mock(return_value=json.dumps({"items": [job]})))
    with pytest.raises(ValueError, match="active"):
        resize.no_active_jobs()


def test_frontend_drain_preserves_two_healthy_exact_survivors(monkeypatch):
    nodes = {"platform-01": "i-one", "platform-02": "i-two", "platform-03": "i-three"}
    targets = [
        {"Target": {"Id": name, "Port": 30443}, "TargetHealth": {"State": "healthy"}}
        for name in nodes.values()
    ]
    assert len(frontend.validate_targets(targets[1:], nodes, "i-one")) == 2
    for bad in (
        [targets[0], targets[0], targets[2]],
        [{**targets[1], "Target": {"Id": "foreign", "Port": 30443}}, targets[2]],
        [{**targets[1], "TargetHealth": {"State": "unhealthy"}}, targets[2]],
    ):
        with pytest.raises(ValueError):
            frontend.validate_targets(bad, nodes, "i-one")
    monkeypatch.setattr(
        frontend,
        "observed",
        lambda _: {"i-one": "draining", "i-two": "healthy", "i-three": "healthy"},
    )
    with pytest.raises(ValueError, match="finish draining"):
        frontend.require_detached({"instance_id": "i-one"})


def test_frontend_waits_for_health_convergence_without_retrying_identity_errors(monkeypatch):
    stable = {"i-two": "healthy", "i-three": "healthy"}
    observe = Mock(side_effect=[frontend.TargetsNotReady("not yet"), stable])
    monkeypatch.setattr(frontend, "observed", observe)

    def bounded_wait(probe, message, *, timeout, interval):
        assert timeout == 180 and interval == 5
        assert probe() is None
        return probe()

    monkeypatch.setattr(frontend, "wait_until", bounded_wait)
    assert frontend.wait_for_survivors({}) == stable
    observe.side_effect = ValueError("Unexpected target identity")
    with pytest.raises(ValueError, match="identity"):
        frontend.wait_for_survivors({})


def test_frontend_never_deregisters_when_survivor_health_deadline_expires(monkeypatch):
    monkeypatch.setattr(frontend, "observed", Mock(side_effect=frontend.TargetsNotReady("late")))

    def deadline(probe, *_args, **_kwargs):
        assert probe() is None
        raise TimeoutError("survivor health deadline")

    monkeypatch.setattr(frontend, "wait_until", deadline)
    mutate = Mock()
    monkeypatch.setattr(frontend, "Aws", mutate)
    with pytest.raises(TimeoutError, match="deadline"):
        frontend.detach({})
    mutate.assert_not_called()
