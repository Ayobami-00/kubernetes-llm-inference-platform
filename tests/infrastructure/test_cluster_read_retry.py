"""Only pure Kubernetes GET observations may retry an ambiguous SSH transport loss."""

from unittest.mock import Mock

import cluster
import pytest


@pytest.mark.parametrize(
    "args", [("get", "node", "platform-01"), ("-n", "observability", "get", "pods")]
)
def test_get_reconnects_once_with_same_target_and_disables_only_multiplexing(monkeypatch, args):
    host = Mock()
    host.run.side_effect = [cluster.CommandFailure("", "ssh", 255), "synthetic-json"]
    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {"synthetic": True}})
    monkeypatch.setattr(cluster, "Host", lambda _: host)
    assert cluster.kubectl(*args) == "synthetic-json"
    assert host.run.call_count == 2
    assert host.run.call_args_list[0].args == host.run.call_args_list[1].args
    assert host.run.call_args_list[1].kwargs == {"reuse": False}


@pytest.mark.parametrize(
    "args",
    [
        ("apply", "-f", "synthetic.yaml"),
        ("create", "configmap", "get"),
        ("-n", "klip", "delete", "pod", "synthetic"),
    ],
)
def test_mutations_are_never_replayed_after_lost_transport(monkeypatch, args):
    host = Mock()
    host.run.side_effect = cluster.CommandFailure("synthetic lost response", "ssh", 255)
    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {}})
    monkeypatch.setattr(cluster, "Host", lambda _: host)
    with pytest.raises(cluster.CommandFailure):
        cluster.kubectl(*args)
    host.run.assert_called_once()


def test_remote_api_errors_are_not_transport_retries(monkeypatch):
    host = Mock()
    host.run.side_effect = cluster.CommandFailure("synthetic forbidden", "ssh", 1)
    monkeypatch.setattr(cluster, "load_hosts", lambda: {"cp-01": {}})
    monkeypatch.setattr(cluster, "Host", lambda _: host)
    with pytest.raises(cluster.CommandFailure):
        cluster.kubectl("get", "nodes")
    host.run.assert_called_once()
