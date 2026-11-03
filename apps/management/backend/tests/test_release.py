"""Release packaging verifies immutable inputs without publishing to a registry."""

import importlib.util
import json
import shutil
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "management_release", ROOT / "scripts/management/release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_release_requires_explicit_matching_tag_and_image_digests(tmp_path):
    assert release.validate("management-v0.2.0") == "0.2.0"
    for tag in ("v0.2.0", "main", "management-v0.2.0-beta", "management-v9.9.9"):
        with pytest.raises(ValueError):
            release.validate(tag)
    with pytest.raises(ValueError):
        release.package(
            "management-v0.2.0", "owner/repo", "latest", "sha256:bad", "commit", tmp_path
        )


@pytest.mark.skipif(not shutil.which("helm"), reason="Helm is required for chart packaging")
def test_packaged_charts_pin_published_digests_without_mutating_source(tmp_path):
    chart = ROOT / "charts/klip/values.yaml"
    original = chart.read_bytes()
    release.package(
        "management-v0.2.0",
        "OWNER/REPO",
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
        "fixture-commit",
        tmp_path,
    )
    manifest = json.loads((tmp_path / "release.json").read_text())
    assert manifest["commit"] == "fixture-commit"
    with tarfile.open(tmp_path / "klip-0.2.0.tgz") as archive:
        values = yaml.safe_load(archive.extractfile("klip/values.yaml"))
    assert values["backend"]["image"] == manifest["images"]["backend"]
    assert values["web"]["image"].endswith("@sha256:" + "b" * 64)
    assert (tmp_path / "klip-postgresql-0.2.0.tgz").exists()
    assert chart.read_bytes() == original


def test_image_stages_workspace_members_outside_packages_without_local_secrets(tmp_path):
    import importlib.util

    location = ROOT / "scripts/management/stage_workspace.py"
    module_spec = importlib.util.spec_from_file_location("stage_workspace", location)
    staging = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(staging)
    source, destination = tmp_path / "source", tmp_path / "image"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        "[tool.uv.workspace]\n"
        'members = ["packages/*", "workload-runner", "dev/inference-stub"]\n'
        'exclude = ["packages/excluded"]\n'
    )
    members = (
        "packages/platform-contracts",
        "packages/excluded",
        "workload-runner",
        "dev/inference-stub",
    )
    for member in members:
        directory = source / member
        directory.mkdir(parents=True)
        (directory / "pyproject.toml").write_text('[project]\nname="example"\nversion="1.0"')
        (directory / "source.py").write_text(
            "# Source remains available to uv's workspace resolver"
        )
        (directory / ".env").write_text("PRIVATE_TEST_MARKER")
        (directory / ".secrets").mkdir()
        (directory / ".secrets/key").write_text("PRIVATE_TEST_MARKER")
    assert set(staging.stage(source, destination)) == set(members) - {"packages/excluded"}
    assert (destination / "dev/inference-stub/source.py").is_file()
    assert not list(destination.rglob(".env")) and not list(destination.rglob(".secrets"))


def test_image_refuses_workspace_paths_outside_build_context(tmp_path):
    module_spec = importlib.util.spec_from_file_location(
        "stage_workspace", ROOT / "scripts/management/stage_workspace.py"
    )
    staging = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(staging)
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers=["outside"]')
    (source / "outside").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="inside the build context"):
        staging.stage(source, tmp_path / "image")
