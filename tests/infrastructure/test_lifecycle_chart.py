"""Management receives the narrow lifecycle client, never privileged host custody."""

import subprocess

import pytest
import yaml
from common import ROOT, tool
from test_lambda_policy import policy


def render(tmp_path, enabled):
    if not (ROOT / "artifacts/tools/bin/helm").is_file():
        pytest.skip("Requires the pinned infrastructure Helm tool")
    values = {
        "backend": {"image": "example.invalid/backend@sha256:" + "a" * 64},
        "web": {"image": "example.invalid/web@sha256:" + "b" * 64},
        "auth": {"existingSecret": "test-management-auth"},
        "database": {"existingSecret": "test-database"},
        "artifacts": {"existingClaim": "test-artifacts"},
        "publicOrigin": "https://management.example.test",
        "lifecycle": {
            "enabled": enabled,
            "bootstrapSeedSecret": "test-bootstrap-seed",
            "policy": policy().model_dump(mode="json"),
        },
    }
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    result = subprocess.run(
        [
            tool("helm"),
            "template",
            "klip",
            str(ROOT / "charts/klip"),
            "--namespace",
            "klip",
            "-f",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


@pytest.mark.parametrize("enabled", [True, False])
def test_lifecycle_mounts_are_restricted_to_api_and_worker(tmp_path, enabled):
    documents = render(tmp_path, enabled)
    for document in documents:
        if document["kind"] not in {"Deployment", "Job"}:
            continue
        spec = document["spec"]["template"]["spec"]
        role = document["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"]
        secrets = {v["secret"]["secretName"] for v in spec.get("volumes", []) if "secret" in v}
        assert not secrets & {"klip-host-credentials", "klip-host-server-tls"}
        expected = enabled and role in {"api", "worker"}
        assert ("klip-host-client-tls" in secrets) == expected
        assert ("test-bootstrap-seed" in secrets) == expected
        if role in {"api", "worker"}:
            environment = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
            assert environment["MANAGEMENT_REAL_LIFECYCLE_ENABLED"] == str(enabled).lower()
            if enabled:
                assert (
                    environment["MANAGEMENT_BOOTSTRAP_SEED_FILE"] == "/run/secrets/bootstrap/seed"
                )
                assert (
                    environment["MANAGEMENT_HOST_EXECUTOR_TLS_HOME"]
                    == "/run/secrets/host-executor-client"
                )
                assert document["spec"]["template"]["metadata"]["annotations"][
                    "klip.dev/lifecycle-policy-sha256"
                ]
    configs = [
        d
        for d in documents
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "klip-lifecycle-policy"
    ]
    assert len(configs) == int(enabled)
