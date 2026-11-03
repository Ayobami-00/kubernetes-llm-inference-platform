import os
import subprocess
import sys


def export(root, *args):
    return subprocess.run(
        [sys.executable, str(root / "scripts/export-contracts.py"), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_schemas_are_current(root):
    result = export(root, "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_export_is_deterministic_and_check_reports_drift_without_writing(root, tmp_path):
    assert export(root, "--output-dir", tmp_path).returncode == 0
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    assert export(root, "--output-dir", tmp_path).returncode == 0
    assert before == {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    path = tmp_path / "customer.schema.json"
    path.write_text(path.read_text() + "\n")
    stale = tmp_path / "stale.schema.json"
    stale.write_text("{}\n")
    before_check = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    result = export(root, "--output-dir", tmp_path, "--check")
    assert result.returncode == 1
    assert "customer.schema.json" in result.stdout and "stale.schema.json" in result.stdout
    assert before_check == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    path.unlink()
    assert export(root, "--output-dir", tmp_path, "--check").returncode == 1


def test_import_does_not_require_service_or_provider_packages():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import platform_contracts, sys; "
                "forbidden = {'fastapi', 'sqlalchemy', 'kubernetes', 'aiperf', 'boto3'}; "
                "assert not forbidden & sys.modules.keys()"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_PROXY": "*"},
    )
    assert result.returncode == 0, result.stderr
