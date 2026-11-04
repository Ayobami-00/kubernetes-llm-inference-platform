"""Exercise real age encryption, ambiguous upload reconciliation and exact-version recovery."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import cluster
import pytest
import telemetry_secrets as custody
from common import ROOT, tool


@pytest.mark.skipif(
    not (ROOT / "artifacts/tools/bin/age").exists() and not shutil.which("age"),
    reason="Requires the checksum-verified age tools; no cloud access",
)
def test_encrypted_backup_reconciles_lost_response_without_reupload_and_restores(
    monkeypatch, tmp_path
):
    folder = tmp_path / "cluster"
    folder.mkdir()
    identity = folder / "backup-identity.txt"
    result = subprocess.run(
        [tool("age-keygen"), "-o", str(identity)], capture_output=True, text=True, check=True
    )
    assert result.returncode == 0
    identity.chmod(0o600)
    key_hash = hashlib.sha256(identity.read_bytes()).hexdigest()
    monkeypatch.setattr(cluster, "FOLDER", folder)
    monkeypatch.setattr(custody, "state_home", lambda: tmp_path)
    monkeypatch.setattr(
        custody,
        "key_custody",
        lambda _: {"sha256": key_hash, "independent_custody_confirmed": True},
    )
    monkeypatch.setattr(
        custody, "tf", lambda *_: json.dumps({"database": {"bucket": "synthetic-private-recovery"}})
    )
    credential = {
        "admin-user": "admin",
        **{
            k: "synthetic-" + k + "-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            for k in ("admin-password", "database-password", "encryption-key")
        },
    }
    path = tmp_path / "credentials/grafana.json"
    path.parent.mkdir()
    path.write_text(json.dumps(credential))

    class Storage:
        data = None
        put_calls = 0
        destination = None

        def __init__(self, *_):
            pass

        def preflight(self, *_):
            pass

        def call(self, service, operation, *args, **kwargs):
            assert service == "s3api"
            assert args[args.index("--bucket") + 1] == "synthetic-private-recovery"
            if operation == "head-object":
                return (
                    None
                    if self.data is None
                    else {
                        "VersionId": "synthetic-v1",
                        "Metadata": {"sha256": hashlib.sha256(self.data).hexdigest()},
                    }
                )
            if operation == "put-object":
                self.put_calls += 1
                self.data = Path(args[args.index("--body") + 1]).read_bytes()
                self.destination = args[args.index("--key") + 1]
                assert self.data.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
                assert all(value.encode() not in self.data for value in credential.values())
                raise RuntimeError("Synthetic response loss after storage accepted ciphertext")
            assert operation == "get-object"
            assert args[args.index("--version-id") + 1] == "synthetic-v1"
            Path(args[-1]).write_bytes(self.data)
            return {"ServerSideEncryption": "AES256"}

    storage = Storage()
    monkeypatch.setattr(custody, "Aws", lambda *_: storage)
    with pytest.raises(RuntimeError, match="Synthetic response loss"):
        custody.operate("backup")
    receipt = custody.operate("backup")
    assert storage.put_calls == 1 and receipt["decrypted_credential_matches"]
    assert storage.destination.startswith("recovery-secrets/grafana-")
    path.unlink()
    custody.operate("restore")
    assert json.loads(path.read_text()) == credential
    assert storage.put_calls == 1
    assert not (tmp_path / "cluster/observability/grafana/download.age").exists()

    def forbid_cloud(*args, **kwargs):
        raise AssertionError("Operator-local recovery must never call a cloud API or Terraform")

    monkeypatch.setattr(custody, "Aws", forbid_cloud)
    monkeypatch.setattr(custody, "tf", forbid_cloud)
    local = custody.stage_local()
    assert local["backup_scope"] == "operator-local-only"
    assert local["decrypted_credential_matches"]
    path.unlink()
    custody.stage_local("restore-local")
    assert json.loads(path.read_text()) == credential
    assert storage.put_calls == 1
