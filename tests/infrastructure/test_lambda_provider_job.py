"""The live setup Job uses application credentials without gaining host or API custody."""

import json

import lambda_provider_setup as setup
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from platform_management.providers.lambda_prerequisites import CPU_NAMES, PrerequisiteSpec
from platform_management.services.common import digest

IMAGE = "127811062476.dkr.ecr.eu-west-2.amazonaws.com/klip/management-backend@sha256:" + "a" * 64


@pytest.fixture
def spec():
    public = Ed25519PrivateKey.from_private_bytes(
        b"test-only-fixed-public-key-source"[:32]
    ).public_key()
    return PrerequisiteSpec(
        kube_system_uid="11111111-2222-3333-4444-555555555555",
        provider_credential_sha256="b" * 64,
        public_key=public.public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        ).decode(),
        cpu_public_ips={n: f"8.8.8.{i}/32" for i, n in enumerate(sorted(CPU_NAMES), 1)},
    )


def test_native_job_has_no_host_credential_or_kubernetes_token_and_cannot_rent(spec):
    config, job, output = setup.manifest(spec, IMAGE, "c" * 20)
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 180
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == {"klip.dev.node-restriction.kubernetes.io/platform": "true"}
    assert pod["securityContext"]["runAsUser"] == 10001
    assert all("projected" not in v and "hostPath" not in v for v in pod["volumes"])
    secrets = {v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v}
    assert secrets == {"klip-management-auth-v1", "klip-db-ca", "klip-host-client-tls"}
    provider = next(v for v in pod["volumes"] if v["name"] == "provider")
    assert provider["secret"]["items"] == [{"key": "lambda-api-key", "path": "lambda-api-key"}]
    assert pod["containers"][0]["command"][:3] == [
        "python",
        "-m",
        "platform_management.jobs.provider_prerequisites",
    ]
    assert pod["containers"][0]["command"][-1] == "/artifacts/" + output
    assert config["immutable"] is True
    assert setup.sha(config["data"]["provider-setup.json"].encode()) == digest(
        spec.model_dump(mode="json")
    )


@pytest.fixture
def plan(spec, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "dev/.secrets").mkdir(parents=True)
    (root / "dev/.secrets/lambda_api_key").write_text("synthetic-provider-secret\n")
    output = tmp_path / "state"
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setattr(setup, "folder", lambda: output)
    monkeypatch.setattr(setup, "source_digest", lambda: "unchanged-source")
    monkeypatch.setattr(
        setup, "image_assessment", lambda *_: {"images": {"backend": {"image": IMAGE}}}
    )
    public = spec.model_dump(mode="json")
    public.pop("provider_credential_sha256")
    # context() returns public metadata only; the actual operator path reads the
    # authoritative executor volume, not a stale Mac peer-registry copy.
    public = {k: public[k] for k in ("kube_system_uid", "public_key", "cpu_public_ips")}
    monkeypatch.setattr(setup, "context", lambda: (None, public, {"cluster_uid": "same"}))
    value = setup.prepare(tmp_path / "receipts")
    return output, value["approval_sha256"]


def test_plan_contains_only_references_and_binds_zero_gpu_envelope(plan):
    root, approval = plan
    directory, value = setup.checked_plan(approval)
    assert directory == root / approval
    assert value["resource_envelope"]["new_gpu_instances"] == 0
    assert "synthetic-provider-secret" not in (directory / "spec.json").read_text()
    assert "synthetic-provider-secret" not in (directory / "job.json").read_text()


@pytest.mark.parametrize("file", ["spec.json", "configmap.json", "job.json", "plan.json"])
def test_changed_frozen_input_is_rejected_before_cluster_dispatch(plan, file):
    root, approval = plan
    path = root / approval / file
    value = json.loads(path.read_text())
    value["injected"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        setup.checked_plan(approval)
