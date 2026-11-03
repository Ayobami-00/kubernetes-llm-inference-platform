"""Upstream helper builds are bounded and cannot install unreviewed executables."""

import base64
import hashlib
import json
from shutil import which
from subprocess import run

import pytest
import yaml
from common import ROOT
from image_tools import job_document, load_lock


def test_build_has_no_credentials_host_access_or_automatic_retry():
    lock = load_lock()
    job = job_document("012345abcdef", lock)
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 1800
    spec = job["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert "hostNetwork" not in spec and "hostPID" not in spec
    assert all(set(v) <= {"name", "configMap", "emptyDir"} for v in spec["volumes"])
    assert spec["nodeSelector"]["klip.dev.node-restriction.kubernetes.io/platform"] == "true"
    container = spec["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert container["resources"]["limits"] == {
        "cpu": "2",
        "memory": "2Gi",
        "ephemeral-storage": "10Gi",
    }
    assert all("valueFrom" not in item for item in container["env"])
    assert "@sha256:" in container["image"]


def test_install_refuses_an_unreviewed_or_changed_binary(tmp_path, monkeypatch):
    import image_tools

    lock = load_lock()
    lock["binary_sha256"] = None
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(lock))
    monkeypatch.setattr(image_tools, "LOCK", path)
    with pytest.raises(ValueError, match="freeze the built helper"):
        image_tools.verified_binary()
    lock["binary_sha256"] = "0" * 64
    path.write_text(json.dumps(lock))
    artifact = tmp_path / "binary"
    artifact.write_bytes(b"altered executable")
    monkeypatch.setattr(image_tools, "CACHE", artifact)
    with pytest.raises(ValueError, match="reviewed binary checksum"):
        image_tools.verified_binary()


def test_a_changed_source_cannot_enter_a_build(tmp_path, monkeypatch):
    import image_tools

    monkeypatch.setattr(image_tools, "ROOT", tmp_path)
    path = tmp_path / "artifacts/platform/ecr-credential-provider-source.tar.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"changed upstream source")
    with pytest.raises(ValueError, match="frozen checksum"):
        image_tools.source_archive(load_lock())


def test_range_export_retries_only_the_same_read_and_rejects_truncation():
    from image_tools import export_binary

    data = b"synthetic binary" * 100000
    calls = []
    failed = False

    def read(*args):
        nonlocal failed
        if "stat" in args:
            return str(len(data))
        if "sha256sum" in args:
            return hashlib.sha256(data).hexdigest() + "  binary"
        command = args[-1]
        assert command.startswith("dd if=/scratch/output/ecr-credential-provider ")
        calls.append(command)
        if not failed:
            failed = True
            raise RuntimeError("read message: unexpected EOF")
        offset = int(command.split("skip=")[1].split()[0]) * 1024 * 1024
        return base64.b64encode(data[offset : offset + 1024 * 1024]).decode()

    assert export_binary(read, "owned-pod") == data
    assert calls[0] == calls[1]

    def truncated(*args):
        return "" if "bash" in args else read(*args)

    with pytest.raises(ValueError, match="truncated"):
        export_binary(truncated, "owned-pod")


def test_image_audience_authorization_is_namespace_and_account_scoped():
    helm = which("helm")
    if not helm:
        pytest.skip("Requires the infrastructure Helm toolchain")
    rendered = run(
        [
            helm,
            "template",
            "klip-image-pull",
            str(ROOT / "charts/klip-image-pull"),
            "--namespace",
            "klip",
            "--set",
            "roleArn=arn:aws:iam::127811062476:role/kubernetes-llm-inference-platform/klip-images-pull",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
    assert {d["kind"] for d in docs} == {"ServiceAccount", "Role", "RoleBinding"}
    rules = next(d for d in docs if d["kind"] == "Role")["rules"]
    assert len(rules) == 1
    assert rules[0]["resources"] == ["sts.amazonaws.com"]
    assert rules[0]["verbs"] == ["request-serviceaccounts-token-audience"]
    assert len(rules[0]["resourceNames"]) == 6
    assert "default" not in rules[0]["resourceNames"]
    assert not any(d["kind"].startswith("Cluster") for d in docs)
