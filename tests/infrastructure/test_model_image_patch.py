"""A serving security patch cannot silently change model dependencies or trust unpinned bytes."""

import hashlib
import importlib.util
import json
from copy import deepcopy
from subprocess import CompletedProcess

import pytest
from common import ROOT

spec = importlib.util.spec_from_file_location(
    "model_image_patch", ROOT / "dev/model-serving/patch.py"
)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


def settings():
    return json.loads((ROOT / "dev/model-serving/patch.lock.json").read_text())


@pytest.mark.parametrize("change", ["url", "sha256", "filename", "size", "name"])
def test_patch_input_tampering_fails_before_downloading(tmp_path, change):
    value = settings()
    value["wheels"][0][change] = {
        "url": "http://files.pythonhosted.org/unsafe.whl",
        "sha256": "unknown",
        "filename": "../../escape.whl",
        "size": 33 * 1024 * 1024,
        "name": "torch",
    }[change]
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        patch.settings(lock)


def test_only_the_reviewed_tools_and_telemetry_may_change():
    before = {**settings()["protected_versions"], "certifi": "old", "pip": "old", "uv": "old"}
    after = {**before, "certifi": "new", "pip": "new", "uv": "new"}
    patch.assert_unchanged(before, after, settings()["protected_versions"])
    with pytest.raises(ValueError, match="dependency"):
        patch.assert_unchanged(before, {**after, "torch": "different"}, {})
    with pytest.raises(ValueError, match="dependency"):
        patch.assert_unchanged(before, {**after, "unreviewed": "1"}, {})
    with pytest.raises(ValueError, match="version"):
        patch.assert_unchanged(before, after, {"vllm": "unexpected"})


def test_cached_wheel_tampering_fails_without_network(tmp_path, monkeypatch):
    wheel = deepcopy(settings()["wheels"][0])
    (tmp_path / wheel["filename"]).write_bytes(b"bad")
    monkeypatch.setattr(patch.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(ValueError, match="checksum/size"):
        patch.fetch(wheel, tmp_path)


def test_download_is_bounded_and_checked_before_install(tmp_path, monkeypatch):
    data = b"small verified wheel"
    wheel = deepcopy(settings()["wheels"][0])
    wheel.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return wheel["url"]

        def read(self, size):
            assert size == len(data) + 1
            return data

    monkeypatch.setattr(patch.urllib.request, "urlopen", lambda *a, **k: Response())
    assert patch.fetch(wheel, tmp_path).read_bytes() == data
    monkeypatch.setattr(Response, "geturl", lambda _: "https://untrusted.example/wheel.whl")
    (tmp_path / wheel["filename"]).unlink()
    with pytest.raises(ValueError, match="redirect"):
        patch.fetch(wheel, tmp_path)


def test_container_base_and_model_versions_match_the_inspected_source():
    value = patch.settings(ROOT / "dev/model-serving/patch.lock.json")
    dockerfile = (ROOT / "dev/model-serving/Dockerfile").read_text()
    assert dockerfile.splitlines()[0] == "FROM " + value["base"]
    lock = json.loads((ROOT / "deploy/inference.lock.json").read_text())
    assert value["base"] == lock["images"][lock["scan_components"]["vllm"]]["image"]
    assert "USER 10001:10001" in dockerfile
    assert "patch.py verify" in dockerfile
    assert "dependency_issues([NCCL_ISSUE])" in (ROOT / "dev/model-serving/patch.py").read_text()


@pytest.mark.parametrize("unexpected", ["extra issue", "", None])
def test_dependency_check_retains_only_the_exact_upstream_nccl_override(monkeypatch, unexpected):
    output = patch.NCCL_ISSUE + "\n" + (unexpected or "")
    code = 0 if unexpected is None else 1
    monkeypatch.setattr(
        patch.subprocess, "run", lambda *a, **k: CompletedProcess([], code, output, "")
    )
    if unexpected or unexpected is None:
        with pytest.raises(ValueError, match="exceptions"):
            patch.dependency_issues([patch.NCCL_ISSUE])
    else:
        assert patch.dependency_issues([patch.NCCL_ISSUE])["issues"] == [patch.NCCL_ISSUE]
