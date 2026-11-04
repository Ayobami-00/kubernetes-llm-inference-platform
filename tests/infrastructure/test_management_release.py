"""Initial credential custody and deployment controls cannot silently drift."""

import copy
import json
from urllib.parse import quote

import management_release as management
import pytest
from common import CONFIG


def test_admin_initialization_never_overwrites_or_replaces_a_live_identity(tmp_path, monkeypatch):
    import cluster

    monkeypatch.setattr(management, "ROOT", tmp_path)
    monkeypatch.setattr(cluster, "kubectl", lambda *a: "")
    assert management.initialize_admin()["created"]
    p = tmp_path / "dev/.secrets/admin_password"
    value = p.read_bytes()
    assert len(value.strip()) == 64 and p.stat().st_mode & 0o777 == 0o600
    assert not management.initialize_admin()["created"]
    assert p.read_bytes() == value
    p.unlink()
    monkeypatch.setattr(cluster, "kubectl", lambda *a: "existing-management")
    with pytest.raises(ValueError, match="recovery"):
        management.initialize_admin()
    assert not p.exists()


def test_database_url_encodes_existing_password_and_keeps_verified_tls(tmp_path, monkeypatch):
    monkeypatch.setattr(management, "ROOT", tmp_path)
    monkeypatch.setattr(management, "state_home", lambda: tmp_path / "operator")
    source = tmp_path / "dev/.secrets"
    source.mkdir(parents=True)
    (source / "admin_password").write_text("synthetic-admin-secret")
    (source / "lambda_api_key").write_text("synthetic-provider-key")
    credential = tmp_path / "operator/credentials/management-postgresql.json"
    credential.parent.mkdir(parents=True)
    password = "synthetic:@/?password" * 3
    credential.write_text(
        json.dumps(
            {"username": "management", "password": password, "cluster_id": CONFIG["cluster_id"]}
        )
    )
    result = management.read_secrets()
    url = result[management.DATABASE]["database-url"]
    assert quote(password, safe="") in url and password not in url
    assert url.endswith("/management?sslmode=verify-full")


def test_production_values_bind_real_runner_and_https_without_cloud_adapters():
    assessment = {
        "images": {
            c: {"image": "example/" + c + "@sha256:" + "1" * 64}
            for c in ("backend", "web", "runner", "stub")
        }
    }
    values = management.values_for(assessment)
    assert values["replicas"] == {"api": 2, "worker": 2, "web": 2}
    assert values["database"]["requireTLS"] and values["web"]["tls"]["enabled"]
    assert values["runner"]["realAdapter"] == "kubernetes:v1"
    assert values["runner"]["simulationAdapter"] == "kubernetes-workload-stub:v1"
    assert values["artifacts"]["existingClaim"] == "klip-artifacts"
    assert not values.get("localDevelopment")
    assert "password" not in json.dumps(values)
    before = copy.deepcopy(assessment)
    values["backend"]["image"] = "modified"
    assert assessment == before
