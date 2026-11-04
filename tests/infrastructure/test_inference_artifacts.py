"""Serving fetches validate frozen bytes before Helm can consume them."""

import json

import inference_artifacts as artifacts
import pytest


@pytest.mark.parametrize("mutation", [None, "image", "platform", "chart", "filename", "source"])
def test_complete_lock_rejects_mutable_images_charts_and_sources(tmp_path, monkeypatch, mutation):
    value = json.loads(artifacts.LOCK.read_text())
    image = next(iter(value["images"].values()))
    chart = next(iter(value["charts"].values()))
    if mutation == "image":
        image["image"] = image["reference"]
    elif mutation == "platform":
        image["platform"] = "linux/arm64"
    elif mutation == "chart":
        chart["version"] = "latest"
    elif mutation == "filename":
        chart["filename"] = "../../escape.tgz"
    elif mutation == "source":
        next(iter(value["artifacts"].values()))["url"] = "http://untrusted.example/source"
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(artifacts, "LOCK", path)
    if mutation:
        with pytest.raises(ValueError):
            artifacts.settings()
    else:
        assert artifacts.settings() == value


def test_tampered_cache_fails_before_any_download(tmp_path, monkeypatch):
    value = artifacts.settings()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / next(iter(value["charts"].values()))["filename"]).write_bytes(b"modified")
    monkeypatch.setattr(artifacts, "CACHE", cache)
    monkeypatch.setattr(artifacts.subprocess, "run", lambda *a, **k: pytest.fail("No download"))
    with pytest.raises(ValueError, match="checksum"):
        artifacts.fetch()
