"""Native host scans must recognize binaries and retain unsuppressed findings."""

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import gpu_host_security as scanner
import pytest


@pytest.mark.parametrize("coverage", [True, False])
def test_binary_scan_uses_rootfs_and_rejects_empty_coverage(tmp_path, monkeypatch, coverage):
    archive = tmp_path / "containerd.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo("bin/containerd")
        entry.size = 4
        bundle.addfile(entry, io.BytesIO(b"test"))
    monkeypatch.setattr(scanner, "install_artifact", lambda: archive)
    monkeypatch.setattr(scanner, "download", lambda item: b"synthetic-runc")
    root = tmp_path / "project"
    (root / "deploy").mkdir(parents=True)
    for name in ("artifacts.lock.json", "gpu-host.lock.json"):
        value = json.loads((scanner.ROOT / "deploy" / name).read_text())
        if name == "artifacts.lock.json":
            value["artifacts"]["runc-linux-amd64"]["sha256"] = hashlib.sha256(
                b"synthetic-runc"
            ).hexdigest()
        (root / "deploy" / name).write_text(json.dumps(value))
    monkeypatch.setattr(scanner, "ROOT", root)
    monkeypatch.setattr(scanner, "tool", lambda name: name)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-secret")
    monkeypatch.setenv("GH_TOKEN", "synthetic-secret")
    monkeypatch.setattr(scanner.subprocess, "check_output", lambda *a, **k: "Version: 0.76.1\n")
    output = tmp_path / "scan"

    def run(args, **kwargs):
        assert not {"AWS_SECRET_ACCESS_KEY", "GH_TOKEN"} & kwargs["env"].keys()
        assert kwargs["check"] and kwargs["timeout"] == 330
        if "--download-db-only" in args:
            cache = Path(kwargs["env"]["TRIVY_CACHE_DIR"]) / "db"
            cache.mkdir(parents=True)
            (cache / "metadata.json").write_text(
                json.dumps({"UpdatedAt": datetime.now(UTC).isoformat()})
            )
            return
        assert args[1] == "rootfs"
        assert args[args.index("--ignorefile") + 1] == "/dev/null"
        names = ("containerd", "containerd-shim-runc-v2", "ctr", "runc") if coverage else ()
        report = {
            "Results": [
                {
                    "Type": "gobinary",
                    "Target": name,
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-test",
                            "PkgName": "stdlib",
                            "InstalledVersion": "v1.0",
                            "Severity": "CRITICAL",
                        }
                    ],
                }
                for name in names
            ]
        }
        (output / "scan.json").write_text(json.dumps(report))

    monkeypatch.setattr(scanner.subprocess, "run", run)
    if not coverage:
        with pytest.raises(ValueError, match="coverage"):
            scanner.scan(output)
        assert (output / "scan.json").exists()
        assert not (output / "summary.json").exists()
        return
    result = scanner.scan(output)
    assert result["critical_findings"] == [("CVE-test", "stdlib", "v1.0")]
    assert result["files"]["runc"] == hashlib.sha256(b"synthetic-runc").hexdigest()
    assert "readiness" in result["limitations"][0]
    with pytest.raises(FileExistsError):
        scanner.scan(output)
