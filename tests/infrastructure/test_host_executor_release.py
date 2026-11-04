"""Custody handoff is bounded, immutable and separate from management authority."""

import base64
import copy
import io
import json
import tarfile

import host_executor_release as release
import pytest
from host_executor_openssh import validate_archive


def test_source_archive_accepts_root_but_rejects_escape_and_links():
    root = tarfile.TarInfo("openssh-10.6p1")
    root.type = tarfile.DIRTYPE
    file = tarfile.TarInfo("openssh-10.6p1/ssh.c")
    file.size = 10
    validate_archive([root, file])
    for path in ("../escape", "openssh-10.6p1/../escape", "/openssh-10.6p1/file", "other/file"):
        with pytest.raises(ValueError):
            validate_archive([tarfile.TarInfo(path)])
    link = tarfile.TarInfo("openssh-10.6p1/link")
    link.type = tarfile.SYMTYPE
    with pytest.raises(ValueError):
        validate_archive([link])


def test_retained_handoff_rejects_missing_changed_or_unexpected_files(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "folder", lambda: tmp_path)
    directory = tmp_path / "handoff"
    directory.mkdir()
    for name in release.FILES:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-public-metadata")
    record = {"files": {n: release.sha((directory / n).read_bytes()) for n in release.FILES}}
    (directory / "manifest.json").write_text(json.dumps(record))
    assert release.handoff() == record
    (directory / "cluster/peers.json").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        release.handoff()


def test_remote_handoff_program_rejects_extra_paths_and_changed_inputs(
    tmp_path, monkeypatch, capsys
):
    import sys

    assert release.STAGE_PROGRAM.count("Path('/executor/state')") == 1
    program = release.STAGE_PROGRAM.replace("Path('/executor/state')", f"Path({str(tmp_path)!r})")
    files = {name: "synthetic-public-data" for name in release.FILES}

    def run(data):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
        exec(compile(program, "frozen-host-handoff", "exec"), {})

    with pytest.raises(ValueError, match="file set"):
        run({"files": {**files, "../escape": "rejected"}})
    assert not (tmp_path / "handoff.json").exists()
    run({"files": files})
    original = (tmp_path / "handoff.json").read_bytes()
    run({"files": files})
    assert (tmp_path / "handoff.json").read_bytes() == original
    changed = {**files, "cluster/peers.json": "changed"}
    with pytest.raises(ValueError, match="differs"):
        run({"files": changed})
    assert (tmp_path / "cluster/peers.json").read_text() == files["cluster/peers.json"]
    capsys.readouterr()


def test_private_secret_staging_compares_without_rotation_or_echo():
    class Transport:
        value, creates = None, 0

        def run(self, args, *, data=None, secret=False):
            assert secret is True
            if "get" in args:
                return json.dumps(self.value) if self.value else ""
            assert "create" in args and self.value is None
            document = json.loads(data)
            self.creates += 1
            self.value = {
                "metadata": {**document["metadata"], "uid": "synthetic-uid"},
                "immutable": document["immutable"],
                "data": {
                    k: base64.b64encode(v.encode()).decode()
                    for k, v in document["stringData"].items()
                },
            }
            return json.dumps(self.value)

    cp, data = Transport(), {"tls.key": "synthetic-test-key"}
    assert release.secret(cp, "platform-ops", "klip-host-server-tls", data) == "synthetic-uid"
    assert release.secret(cp, "platform-ops", "klip-host-server-tls", data) == "synthetic-uid"
    assert cp.creates == 1
    with pytest.raises(ValueError, match="differs"):
        release.secret(cp, "platform-ops", "klip-host-server-tls", {"tls.key": "different"})
    cp.value["metadata"]["annotations"]["klip.dev/project"] = "foreign"
    with pytest.raises(ValueError):
        release.secret(cp, "platform-ops", "klip-host-server-tls", data)
    assert cp.creates == 1


def test_preflight_job_has_no_cluster_token_or_api_client_key():
    pod = {
        "metadata": {"labels": {}},
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 20002,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "api",
                    "image": "synthetic-pinned-image",
                    "env": [
                        {"name": "LC_ALL", "value": "C.UTF-8"},
                        {"name": "LANGUAGE", "value": "C"},
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [
                        {"name": "state", "mountPath": "/executor/state"},
                        {"name": "tls", "mountPath": "/server-only"},
                        {"name": "identity", "mountPath": "/api-only"},
                    ],
                }
            ],
            "volumes": [
                {"name": "state", "persistentVolumeClaim": {"claimName": "klip-host-state"}},
                {"name": "tls", "secret": {"secretName": "klip-host-server-tls"}},
                {"name": "identity", "projected": {"sources": []}},
            ],
        },
    }
    original = copy.deepcopy(pod)
    job = release.bootstrap_job(
        {"approval_sha256": "a" * 64}, [{"kind": "Deployment", "spec": {"template": pod}}]
    )
    actual = job["spec"]["template"]["spec"]
    assert (
        actual["serviceAccountName"] == "klip-host-job"
        and actual["automountServiceAccountToken"] is False
    )
    assert job["spec"]["backoffLimit"] == 0
    assert actual["containers"][0]["resources"] == {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }
    secrets = [v["secret"]["secretName"] for v in actual["volumes"] if "secret" in v]
    assert secrets == ["klip-host-credentials"]
    tokens = [v for v in actual["volumes"] if "projected" in v]
    assert tokens[0]["projected"]["sources"] == [
        {
            "serviceAccountToken": {
                "audience": "sts.amazonaws.com",
                "expirationSeconds": 900,
                "path": "token",
            }
        }
    ]
    assert pod == original
    mounts = [
        v["mountPath"].replace("/var/run/", "/run/")
        for v in actual["containers"][0]["volumeMounts"]
    ]
    assert len(mounts) == len(set(mounts))


def test_network_update_cannot_change_images_or_add_broader_access():
    import yaml
    from common import ROOT
    from host_executor_network import validate_delta

    original = {
        "kind": "Deployment",
        "metadata": {"name": "host-executor"},
        "spec": {"image": "immutable-fixture"},
    }
    policy = list(
        yaml.safe_load_all(
            (ROOT / "charts/klip-host-executor/templates/api-access.yaml").read_text()
        )
    )[0]
    old = yaml.safe_dump(original)
    candidate = yaml.safe_dump_all([original, policy])
    validate_delta(old, candidate)
    changed = copy.deepcopy(original)
    changed["spec"]["image"] = "other"
    with pytest.raises(ValueError):
        validate_delta(old, yaml.safe_dump_all([changed, policy]))
    wide = copy.deepcopy(policy)
    wide["spec"]["egress"][0]["toEntities"] = ["cluster"]
    with pytest.raises(ValueError):
        validate_delta(old, yaml.safe_dump_all([original, wide]))
    with pytest.raises(ValueError, match="Duplicate"):
        validate_delta(old, yaml.safe_dump_all([original, policy, policy]))


def test_management_namespace_probe_has_only_client_tls_and_no_api_or_ssh_credentials():
    from host_executor_acceptance import PROGRAM, probe_job
    from image_release import guard_workloads

    compile(PROGRAM, "tls-boundary-probe", "exec")
    job = probe_job("host-service-probe-synthetic", "reviewed-image", "synthetic-cluster-uid")
    guard_workloads([job])
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert {v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v} == {
        "klip-host-client-tls"
    }
    assert not any("persistentVolumeClaim" in v or "projected" in v for v in pod["volumes"])
    assert job["spec"]["backoffLimit"] == 0
