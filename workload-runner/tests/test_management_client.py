import hashlib
import json

import httpx
import pytest
from workload_runner.management import ManagementClient


def test_management_artifact_collection_checks_content(tmp_path, repo):
    password = tmp_path / "password"
    password.write_text("private-management-password")
    fixture = json.loads(
        (repo / "contracts/fixtures/runs/writer-assist/completed-passing.json").read_text()
    )
    content = b"# Synthetic report\n"
    sha = hashlib.sha256(content).hexdigest()
    fixture["result"]["artifacts"] = {
        "report": {"uri": "file:///artifacts/report.md", "sha256": sha}
    }
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/auth/login"):
            assert json.loads(request.content)["password"] == password.read_text()
            return httpx.Response(200, json={"csrf_token": "csrf"})
        assert request.headers["X-CSRF-Token"] == "csrf"
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "registered",
                        "uri": "file:///artifacts/report.md",
                        "sha256": sha,
                        "status": "verified",
                    }
                ],
            )
        return httpx.Response(200, content=content)

    client = ManagementClient(
        "http://management.test", password, transport=httpx.MockTransport(handler)
    )
    try:
        run = client.collect({"run": fixture}, tmp_path / "evidence")
    finally:
        client.close()
    evidence = json.loads((tmp_path / "evidence/evidence.json").read_text())
    downloaded = evidence["artifacts"]["report"]["path"]
    from pathlib import Path

    assert Path(downloaded).is_relative_to(tmp_path / "evidence/downloads")
    assert Path(downloaded).read_bytes() == content and run.status == "completed"
    assert password.read_text() not in (tmp_path / "evidence/management-run.json").read_text()


def test_embedded_management_credentials_rejected(tmp_path):
    with pytest.raises(ValueError, match="embed credentials"):
        ManagementClient("http://admin:secret@management.test", tmp_path / "unused")


def test_observation_tolerates_api_replacement_without_relaunch(tmp_path, monkeypatch):
    password = tmp_path / "password"
    password.write_text("secret")
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.url.path.endswith("login"):
            return httpx.Response(200, json={"csrf_token": "csrf"})
        return (
            httpx.Response(503)
            if len(calls) == 2
            else httpx.Response(200, json={"status": "completed"})
        )

    monkeypatch.setattr("workload_runner.management.time.sleep", lambda _: None)
    client = ManagementClient(
        "http://management.test", password, transport=httpx.MockTransport(handler)
    )
    try:
        assert client.wait("imports/existing", "status", {"completed"})["status"] == "completed"
        assert calls == ["POST", "GET", "GET"]
    finally:
        client.close()


def test_renamed_or_unverified_result_artifact_cannot_be_collected(tmp_path, repo):
    password = tmp_path / "password"
    password.write_text("secret")
    fixture = json.loads(
        (repo / "contracts/fixtures/runs/writer-assist/completed-passing.json").read_text()
    )
    fixture["result"]["artifacts"] = {
        "report": {"uri": "file:///artifacts/report.md", "sha256": "f" * 64}
    }

    def handler(request):
        return httpx.Response(
            200, json={"csrf_token": "csrf"} if request.url.path.endswith("login") else []
        )

    client = ManagementClient(
        "http://management.test", password, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ValueError, match="not verified"):
            client.collect({"run": fixture}, tmp_path / "evidence")
    finally:
        client.close()
