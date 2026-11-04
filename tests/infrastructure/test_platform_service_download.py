"""A fresh OCI fetch must not depend on Helm's chosen archive filename."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("tampered", [False, True])
def test_fresh_oci_cache_uses_verified_content_and_a_stable_filename(
    tmp_path, monkeypatch, tampered
):
    import platform_services as services

    archive = io.BytesIO()
    chart = yaml.safe_dump({"name": "cert-manager", "version": "v1.21.3"}).encode()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        entry = tarfile.TarInfo("cert-manager/Chart.yaml")
        entry.size = len(chart)
        output.addfile(entry, io.BytesIO(chart))
    body = archive.getvalue()
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "charts": {
                    "cert-manager": {
                        "version": "v1.21.3",
                        "file": "cert-manager-v1.21.3.tgz",
                        "oci": "oci://quay.io/jetstack/charts/cert-manager@sha256:" + "a" * 64,
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                }
            }
        )
    )
    cache = tmp_path / "empty-cache"
    monkeypatch.setattr(services, "LOCK", lock)
    monkeypatch.setattr(services, "CACHE", cache)
    monkeypatch.setattr(services, "tool", lambda _: "helm-fixture")

    def pull(arguments, *, check):
        assert check and arguments[1] == "pull"
        destination = Path(arguments[arguments.index("--destination") + 1])
        assert destination != cache
        (destination / "cert-manager-another-filename.tgz").write_bytes(
            b"changed archive" if tampered else body
        )

    monkeypatch.setattr(services.subprocess, "run", pull)
    if tampered:
        with pytest.raises(ValueError, match="checksum differs"):
            services.fetch()
        assert not (cache / "cert-manager-v1.21.3.tgz").exists()
    else:
        services.fetch()
        assert (cache / "cert-manager-v1.21.3.tgz").read_bytes() == body
        assert (
            yaml.safe_load((cache / "cert-manager/Chart.yaml").read_text())["version"] == "v1.21.3"
        )
