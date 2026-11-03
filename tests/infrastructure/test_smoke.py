"""Outage tests must recover the stopped host and remove every temporary listener."""

import json

import pytest
import smoke


class Cloud:
    def __init__(self, allocation, *, deny_stop=False):
        self.nodes = allocation["nodes"]
        self.state = "running"
        self.deny_stop = deny_stop
        self.restarts = 0

    def preflight(self, root):
        return {}

    def call(self, service, operation, *args):
        if operation == "describe-target-health":
            return {
                "TargetHealthDescriptions": [
                    {
                        "Target": {"Id": node["private_ip"]},
                        "TargetHealth": {
                            "State": "unhealthy"
                            if name == "cp-03" and self.state == "stopped"
                            else "healthy"
                        },
                    }
                    for name, node in self.nodes.items()
                ]
            }
        if operation == "stop-instances":
            if self.deny_stop:
                raise RuntimeError("Stop denied")
            self.state = "stopped"
            return {}
        if operation == "start-instances":
            self.state = "running"
            self.restarts += 1
            return {}
        if operation == "describe-instances":
            return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}
        if operation == "describe-instance-information":
            return {"InstanceInformationList": [{"PingStatus": "Online"}]}
        raise AssertionError((service, operation))


def setup(monkeypatch, tmp_path, allocation, *, deny_stop=False, guard_error=False):
    cloud = Cloud(allocation, deny_stop=deny_stop)
    calls = []
    monkeypatch.setenv("KLIP_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(smoke, "Aws", lambda profile: cloud)

    def command(aws, instance, script):
        calls.append((instance, script))
        if guard_error and "Refusing prebootstrap" in script:
            raise ValueError("Kubernetes already exists")
        if "urllib.request" in script:
            count = 30 if "range(30)" in script else 20
            return {"stdout": json.dumps({"success": ["cp-01"] * count, "errors": []})}
        return {"stdout": ""}

    monkeypatch.setattr(smoke, "ssm_command", command)
    return cloud, calls


def test_restart_recreates_runtime_listener_then_removes_all(monkeypatch, tmp_path, allocation):
    cloud, calls = setup(monkeypatch, tmp_path, allocation)
    report = smoke.smoke(allocation)
    assert report["passed"]
    assert cloud.state == "running" and cloud.restarts == 1
    cp3 = allocation["nodes"]["cp-03"]["instance_id"]
    assert sum(i == cp3 and "systemd-run" in script for i, script in calls) == 2
    assert sum("systemctl stop" in script for _, script in calls) == 3


def test_denied_stop_still_cleans_up_without_waiting_for_nonexistent_stop(
    monkeypatch, tmp_path, allocation
):
    cloud, calls = setup(monkeypatch, tmp_path, allocation, deny_stop=True)
    with pytest.raises(RuntimeError, match="Stop denied"):
        smoke.smoke(allocation)
    assert cloud.state == "running" and cloud.restarts == 0
    assert sum("systemctl stop" in script for _, script in calls) == 3
    report = json.loads((tmp_path / "reports/smoke.json").read_text())
    assert not report["passed"]


def test_existing_cluster_prevents_listener_and_outage(monkeypatch, tmp_path, allocation):
    cloud, calls = setup(monkeypatch, tmp_path, allocation, guard_error=True)
    with pytest.raises(RuntimeError, match="Kubernetes already exists"):
        smoke.smoke(allocation)
    assert not any("systemd-run" in script for _, script in calls)
    assert cloud.restarts == 0 and cloud.state == "running"


def test_cleanup_failure_is_reported_as_failure(monkeypatch, tmp_path, allocation):
    cloud, calls = setup(monkeypatch, tmp_path, allocation)
    original = smoke.ssm_command

    def failing_cleanup(aws, instance, script):
        if "systemctl stop" in script:
            calls.append((instance, script))
            raise RuntimeError("cleanup failed")
        return original(aws, instance, script)

    monkeypatch.setattr(smoke, "ssm_command", failing_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        smoke.smoke(allocation)
    assert cloud.state == "running"
    report = json.loads((tmp_path / "reports/smoke.json").read_text())
    assert not report["passed"] and len(report["cleanup_errors"]) == 3
