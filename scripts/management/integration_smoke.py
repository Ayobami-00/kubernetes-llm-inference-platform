"""Stdlib-only operator for the isolated kind integration proof. Prints no secrets."""

import hashlib
import http.cookiejar
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


def setup(root):
    password = secrets.token_urlsafe(32)
    (root / "admin_password").write_text(password)
    db_password = secrets.token_urlsafe(32)
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "klip-test-auth", "namespace": "klip"},
        "stringData": {
            "postgres-password": secrets.token_urlsafe(32),
            "app-password": db_password,
            "database-url": "postgresql+psycopg://klip:"
            + quote(db_password)
            + "@klip-db:5432/klip",
            "admin-password": password,
            "lambda-api-key": "",
        },
    }
    resources = [secret]
    for name, mode, path in (
        ("db", "ReadWriteOnce", "/var/klip-db"),
        ("artifacts", "ReadWriteMany", "/var/klip-artifacts"),
    ):
        resources.extend(
            [
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolume",
                    "metadata": {"name": "klip-test-" + name},
                    "spec": {
                        "capacity": {"storage": "2Gi"},
                        "accessModes": [mode],
                        "storageClassName": "klip-test",
                        "persistentVolumeReclaimPolicy": "Retain",
                        "hostPath": {"path": path},
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "klip-test-" + name, "namespace": "klip"},
                    "spec": {
                        "accessModes": [mode],
                        "storageClassName": "klip-test",
                        "volumeName": "klip-test-" + name,
                        "resources": {"requests": {"storage": "2Gi"}},
                    },
                },
            ]
        )
    (root / "resources.json").write_text(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": resources})
    )
    (root / "values.json").write_text(
        json.dumps(
            {
                "backend": {"image": "klip-integration-backend:local"},
                "web": {
                    "image": "klip-integration-web:local",
                    "serviceType": "NodePort",
                    "nodePort": 31080,
                },
                "database": {"existingSecret": "klip-test-auth"},
                "auth": {"existingSecret": "klip-test-auth"},
                "publicOrigin": os.environ["MANAGEMENT_WEB_URL"],
                "localDevelopment": True,
                "artifacts": {"existingClaim": "klip-test-artifacts"},
                "runner": {
                    "simulationAdapter": "kubernetes-fixture:v1",
                    "image": "klip-integration-backend:local",
                    "servingNamespace": "llm-d",
                },
            }
        )
    )


class API:
    def __init__(self, root):
        self.root = root
        self.url = os.environ["MANAGEMENT_WEB_URL"]
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.csrf = ""
        info = self.request(
            "POST",
            "/auth/login",
            {"username": "admin", "password": (root / "admin_password").read_text().strip()},
        )
        self.csrf = info["csrf_token"]

    def request(self, method, path, body=None, key=None):
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers.update(
                {
                    "X-CSRF-Token": self.csrf,
                    "Origin": self.url,
                    "Idempotency-Key": key or secrets.token_hex(16),
                }
            )
        request = urllib.request.Request(
            self.url + "/api/v1" + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise AssertionError("API " + path + ": " + exc.read().decode()) from exc

    def wait(self, path, key, expected, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.request("GET", path)
            value = data
            for part in key.split("."):
                value = value[part]
            if value == expected:
                return data
            if value == "failed":
                raise AssertionError("Workflow failed: " + json.dumps(data))
            time.sleep(1)
        raise AssertionError("Timed out: " + path)

    def import_document(self, kind, document):
        result = self.request(
            "POST",
            "/imports/" + kind,
            {
                "source": "synthetic-integration-proof",
                "provenance": "synthetic",
                "document": document,
            },
        )
        return self.wait("/imports/" + result["import_id"], "status", "completed")


def wait_ready(url):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/health/ready", timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.HTTPError):
            pass
        time.sleep(1)
    raise AssertionError("Application readiness timed out")


def exercise(root):
    wait_ready(os.environ["MANAGEMENT_WEB_URL"])
    api = API(root)
    for endpoint, expected in (
        ("/customers", 5),
        ("/scenarios", 7),
        ("/worker-group-specs", 3),
        ("/serving-profiles", 4),
        ("/datasets", 6),
    ):
        assert len(api.request("GET", endpoint)) == expected
    fixture = json.loads(
        (ROOT / "contracts/fixtures/runs/writer-assist/completed-passing.json").read_text()
    )
    inventory = json.loads((ROOT / "contracts/fixtures/inventories/cluster.json").read_text())
    api.import_document("inventory", inventory)
    artifact = root / "data/artifacts/datasets/integration.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'{"fixture":true,"text":"synthetic contract proof"}\n')
    snapshot = fixture["snapshot"]
    snapshot["datasets"][0]["prepared"] = {
        "uri": "file:///artifacts/datasets/integration.jsonl",
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "record_count": 1,
        "tokenizer_revision": "fixture-tokenizer",
        "prompt_template_sha256": "0" * 64,
        "max_rendered_input_tokens": 1,
    }
    api.import_document(
        "prepared-runs", {"id": "fixture-kind-prepared", "fixture": True, "snapshot": snapshot}
    )
    external = json.loads(
        (ROOT / "contracts/fixtures/runs/writer-assist/completed-violating.json").read_text()
    )
    external["id"] = "external-integration-result"
    api.import_document("runs", external)
    bad = api.request(
        "POST",
        "/runs/preflight",
        {
            "scenario_ref": "writer-assist-baseline@1",
            "mode": "real",
            "prepared_run_id": "fixture-kind-prepared",
        },
    )
    assert not bad["executable"] and bad["checks"]
    group = api.request(
        "POST", "/worker-groups", {"id": "integration-group", "spec_ref": "latency-workers@1"}
    )
    api.wait("/operations/" + group["operation_id"], "operation.status", "completed")
    serving = api.request(
        "POST",
        "/serving-deployments",
        {"id": "gec", "profile_ref": "gec-baseline@1", "group_id": "integration-group"},
    )
    api.wait("/operations/" + serving["operation_id"], "operation.status", "completed")
    launched = api.request(
        "POST",
        "/runs",
        {
            "scenario_ref": "writer-assist-baseline@1",
            "mode": "simulation",
            "prepared_run_id": "fixture-kind-prepared",
            "group_ids": {"latency-workers@1": "integration-group"},
            "outcome": "violating",
        },
    )
    run_id = launched["resource_id"]
    # Wait for an actual Job before replacing both durable worker Pods.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        jobs = json.loads(
            run(
                "kubectl",
                "-n",
                "klip",
                "get",
                "jobs",
                "-l",
                "klip.dev/run-id=" + run_id,
                "-o",
                "json",
            )
        )["items"]
        if jobs:
            break
        time.sleep(0.5)
    else:
        raise AssertionError("Runner Job was not created")
    uid = jobs[0]["metadata"]["uid"]
    run("kubectl", "-n", "klip", "rollout", "restart", "deployment/klip-worker")
    run("kubectl", "-n", "klip", "rollout", "status", "deployment/klip-worker", "--timeout=120s")
    completed = api.wait("/runs/" + run_id, "run.status", "completed")
    assert completed["run"]["fixture"] and completed["run"]["result"]["slo_verdict"] == "violating"
    final_jobs = json.loads(
        run("kubectl", "-n", "klip", "get", "jobs", "-l", "klip.dev/run-id=" + run_id, "-o", "json")
    )["items"]
    assert len(final_jobs) == 1 and final_jobs[0]["metadata"]["uid"] == uid
    invocations = list((root / "data/artifacts/runs" / run_id).glob("*/fixture-invocations.log"))
    assert len(invocations) == 1 and len(invocations[0].read_text().splitlines()) == 1
    frozen = completed["run"]["snapshot"]
    (root / "expected.json").write_text(json.dumps({"run_id": run_id, "snapshot": frozen}))
    print(
        "Synthetic Job completed with one invocation and the original UID after worker replacement."
    )


def verify(root):
    wait_ready(os.environ["MANAGEMENT_WEB_URL"])
    expected = json.loads((root / "expected.json").read_text())
    api = API(root)
    stored = api.request("GET", "/runs/" + expected["run_id"])
    assert stored["run"]["snapshot"] == expected["snapshot"]
    assert stored["run"]["status"] == "completed"
    assert any(
        r["source"] == "synthetic-integration-proof" and r["status"] == "completed"
        for r in api.request("GET", "/imports")
    )
    assert api.request("GET", "/prepared-runs")[0]["id"] == "fixture-kind-prepared"
    assert api.request("GET", "/inventory")
    print("Historical snapshot, imports and results survived upgrade and database Pod replacement.")


def verify_restore(root):
    # Compare source/restore counts and the immutable snapshot through separate psql calls.
    counts = []
    for database in ("klip", "klip_restore_test"):
        tables = (
            "specifications",
            "revisions",
            "runs",
            "import_receipts",
            "inventory_snapshots",
            "prepared_runs",
            "execution_bindings",
        )
        query = (
            "SELECT jsonb_build_array("
            + ",".join("(SELECT count(*) FROM " + table + ")" for table in tables)
            + ")"
        )
        counts.append(
            run(
                "kubectl",
                "-n",
                "klip",
                "exec",
                "klip-db-0",
                "--",
                "psql",
                "-U",
                "postgres",
                "-d",
                database,
                "-tAc",
                query,
            ).strip()
        )
    assert counts[0] == counts[1] and all(n > 0 for n in json.loads(counts[0]))
    expected = json.loads((root / "expected.json").read_text())
    restored = run(
        "kubectl",
        "-n",
        "klip",
        "exec",
        "klip-db-0",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "klip_restore_test",
        "-tAc",
        "SELECT snapshot FROM runs WHERE id='" + expected["run_id"] + "'",
    )
    assert json.loads(restored) == expected["snapshot"]
    print("Logical backup restored the complete run catalogue and frozen snapshot.")


if __name__ == "__main__":
    action, path = sys.argv[1:3]
    {"setup": setup, "exercise": exercise, "verify": verify, "verify-restore": verify_restore}[
        action
    ](Path(path))
