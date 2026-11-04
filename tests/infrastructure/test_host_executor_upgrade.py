"""Executor updates cannot overwrite custody or cross an active writer's fence."""

import copy
import fcntl
import json

import cluster
import host_executor_upgrade as upgrade
import pytest
import yaml
from host_executor_release import CPU_NAMES

OLD = "127811062476.dkr.ecr.eu-west-2.amazonaws.com/klip/management-backend@sha256:" + "a" * 64
NEW = OLD.replace("a" * 64, "b" * 64)


def documents(image):
    return [
        {"kind": "Secret", "metadata": {"name": "unchanged"}, "stringData": {"test": "fixture"}},
        {
            "kind": "Deployment",
            "metadata": {"name": "host-executor"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "image": image,
                                "env": [{"name": "HOST_EXECUTOR_IMAGE", "value": image}],
                                "securityContext": {"readOnlyRootFilesystem": True},
                            }
                        ]
                    }
                }
            },
        },
    ]


def test_update_allows_exact_image_delta_only():
    before, after = documents(OLD), documents(NEW)
    upgrade.validate_delta(yaml.safe_dump_all(before), yaml.safe_dump_all(after), OLD, NEW)
    changes = [copy.deepcopy(after) for _ in range(5)]
    changes[0][0]["stringData"]["test"] = "changed"
    changes[1][1]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["privileged"] = (
        True
    )
    changes[2][1]["spec"]["template"]["spec"]["hostNetwork"] = True
    changes[3][1]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = OLD
    changes[4].append({"kind": "Role", "metadata": {"name": "extra"}})
    for value in changes:
        with pytest.raises(ValueError):
            upgrade.validate_delta(yaml.safe_dump_all(before), yaml.safe_dump_all(value), OLD, NEW)


def pods(image):
    return [
        {
            "metadata": {"uid": str(i)},
            "spec": {
                "nodeName": "platform-0" + str(i),
                "containers": [{"image": image}],
                "nodeSelector": {"klip.dev.node-restriction.kubernetes.io/platform": "true"},
            },
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"ready": True, "imageID": "containerd://" + image}],
            },
        }
        for i in (1, 2)
    ]


@pytest.mark.parametrize("change", ["same-host", "digest", "not-ready", "placement", "missing"])
def test_observed_replicas_require_actual_digest_and_distinct_cpu_hosts(change):
    value = pods(NEW)
    assert len(upgrade.ready_pods(value, NEW)) == 2
    if change == "same-host":
        value[1]["spec"]["nodeName"] = value[0]["spec"]["nodeName"]
    elif change == "digest":
        value[1]["status"]["containerStatuses"][0]["imageID"] = OLD
    elif change == "not-ready":
        value[1]["status"]["conditions"] = []
    elif change == "placement":
        value[1]["spec"]["nodeSelector"] = {}
    else:
        value.pop()
    with pytest.raises(ValueError):
        upgrade.ready_pods(value, NEW)


@pytest.fixture
def guard(tmp_path, monkeypatch):
    (tmp_path / "cluster").mkdir()
    (tmp_path / "executions").mkdir()
    (tmp_path / "cluster/peers.json").write_text(
        json.dumps({"revision": 12, "peers": {n: {} for n in CPU_NAMES}})
    )
    monkeypatch.setenv("KLIP_UPGRADE_ID", "test-upgrade")
    monkeypatch.setenv("POD_UID", "original-pod")
    code = upgrade.GUARD_PROGRAM.replace("Path('/executor/state')", f"Path({str(tmp_path)!r})")
    return tmp_path, code


def test_guard_holds_both_authoritative_locks_until_explicit_release(guard, monkeypatch):
    import time

    root, code = guard
    observed = []

    def check(_):
        for name in ("dispatch.lock", "cluster/operator.lock"):
            with (root / name).open("a") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append(name)
        (root / "upgrades/test-upgrade/release").touch()

    monkeypatch.setattr(time, "sleep", check)
    exec(compile(code, "native-upgrade-guard", "exec"), {})
    assert len(observed) == 2
    proof = json.loads((root / "upgrades/test-upgrade/guard.json").read_text())
    assert proof["pod_uid"] == "original-pod" and proof["registry_revision"] == 12
    with (root / "dispatch.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("problem", ["writer", "dispatch", "gpu-peer", "unfinished", "cleanup"])
def test_guard_refuses_active_or_unresolved_ownership(guard, problem):
    from contextlib import ExitStack

    root, code = guard
    with ExitStack() as stack:
        if problem in {"writer", "dispatch"}:
            name = "cluster/operator.lock" if problem == "writer" else "dispatch.lock"
            stream = stack.enter_context((root / name).open("a"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif problem == "gpu-peer":
            path = root / "cluster/peers.json"
            value = json.loads(path.read_text())
            value["peers"]["gpu-extra"] = {}
            path.write_text(json.dumps(value))
        else:
            directory = root / "executions/host-example"
            directory.mkdir()
            (directory / "task.json").write_text("{}")
            if problem == "cleanup":
                (directory / "observation.json").write_text(
                    json.dumps({"status": "failed", "cleanup_pending": True})
                )
        with pytest.raises((ValueError, BlockingIOError)):
            exec(compile(code, "native-upgrade-guard", "exec"), {})
    assert not (root / "upgrades/test-upgrade/guard.json").exists()


@pytest.fixture
def plan(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, "folder", lambda: tmp_path)
    monkeypatch.setattr(upgrade, "assess", lambda *_: {"image": NEW})
    monkeypatch.setattr(upgrade, "checked_image_source", lambda *_: None)
    monkeypatch.setattr(upgrade, "custody", lambda: {"retained": "same"})
    monkeypatch.setattr(upgrade, "source_digest", lambda: "source-sha")
    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {}})
    monkeypatch.setattr(upgrade, "identities", lambda *_: ({"cluster_uid": "same"}, pods(OLD)))
    monkeypatch.setattr(
        upgrade,
        "rendered",
        lambda p: (yaml.safe_dump_all(documents(yaml.safe_load(p.read_text())["image"])), []),
    )
    (tmp_path / "active.json").write_text(json.dumps({"image": OLD}))
    (tmp_path / "values.yaml").write_text(yaml.safe_dump({"image": OLD}))
    (tmp_path / "rendered.yaml").write_text(yaml.safe_dump_all(documents(OLD)))
    value = upgrade.prepare(tmp_path / "receipt.json", tmp_path / "security")
    return tmp_path, value["approval_sha256"]


def test_saved_plan_keeps_custody_and_release_immutable_before_application(plan):
    root, approval = plan
    directory, value = upgrade.checked_plan(approval)
    assert directory == root / "upgrades" / approval
    assert value["custody_fingerprints"] == {"retained": "same"}
    assert json.loads((root / "active.json").read_text())["image"] == OLD


@pytest.mark.parametrize(
    "name",
    [
        "plan.json",
        "values.yaml",
        "rendered.yaml",
        "previous-active.json",
        "previous-values.yaml",
        "previous-rendered.yaml",
    ],
)
def test_altered_saved_upgrade_fails_before_any_native_create(plan, name):
    root, approval = plan
    path = root / "upgrades" / approval / name
    path.write_text(path.read_text() + "\n# altered")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        upgrade.checked_plan(approval)
