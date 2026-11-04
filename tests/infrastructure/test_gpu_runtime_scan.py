"""Runtime-image evidence is retained even when scanning finds a blocking issue."""

import json
from types import SimpleNamespace

import gpu_runtime_scan as scanner
import pytest


@pytest.mark.parametrize("problem", [None, "critical", "coverage", "identity"])
def test_scan_rejects_invalid_evidence_and_retains_findings(tmp_path, monkeypatch, problem):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-value")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "synthetic-test-value")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-test-value")
    reference = next(iter(json.loads(scanner.LOCK.read_text())["images"]))
    pinned = reference + "@" + json.loads(scanner.LOCK.read_text())["images"][reference]["digest"]
    output = tmp_path / "reports"

    def run(arguments, **options):
        if arguments == ["trivy", "--version"]:
            return SimpleNamespace(stdout="Version: 0.76.1")
        env = options["env"]
        assert not any(
            k in env for k in ("AWS_SECRET_ACCESS_KEY", "DOCKER_AUTH_CONFIG", "GITHUB_TOKEN")
        )
        if "--download-db-only" in arguments:
            path = tmp_path / "gpu-scan-cache/db"
            path.mkdir(parents=True)
            (path / "metadata.json").write_text('{"UpdatedAt":"2026-11-04T06:00:00Z"}')
        else:
            assert arguments[-1] == pinned
            data = {
                "ArtifactName": "wrong" if problem == "identity" else pinned,
                "Metadata": {"ImageConfig": {"architecture": "amd64"}},
                "Results": [
                    {
                        "Type": "gobinary",
                        "Target": "binary",
                        "Packages": [] if problem == "coverage" else [{"Name": "stdlib"}],
                        "Vulnerabilities": [{"Severity": "CRITICAL"}]
                        if problem == "critical"
                        else [],
                    }
                ],
            }
            (output / "scan.json").write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    if problem:
        with pytest.raises(ValueError):
            scanner.scan(reference, output)
        assert (
            json.loads((output / "summary.json").read_text())["status"]
            == "review-or-reconciliation-required"
        )
    else:
        assert scanner.scan(reference, output)["status"] == "scanned"
        with pytest.raises(FileExistsError):
            scanner.scan(reference, output)


def test_scan_refuses_unpinned_references_before_creating_output(tmp_path):
    output = tmp_path / "unused"
    with pytest.raises(ValueError, match="checked GPU lock"):
        scanner.scan("nvcr.io/nvidia/driver:latest", output)
    assert not output.exists()
