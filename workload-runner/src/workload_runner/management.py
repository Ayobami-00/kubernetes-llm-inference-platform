"""Management API client for operator launches; never another management service."""

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from platform_contracts import Run, RunSnapshot

from .artifacts import atomic_json, canonical


class ManagementClient:
    def __init__(self, url, password_file, *, transport=None):
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("management URL must not embed credentials or query parameters")
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/api/v1/", timeout=30, trust_env=False, transport=transport
        )
        try:
            login = self.request(
                "POST",
                "auth/login",
                {
                    "username": "admin",
                    "password": Path(password_file).read_text().strip(),
                },
            )
            self.client.headers["X-CSRF-Token"] = login["csrf_token"]
        except Exception:
            self.close()
            raise

    def close(self):
        self.client.close()

    def request(self, method, path, body=None, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        response = self.client.request(method, path, json=body, headers=headers)
        if not response.is_success:
            raise ValueError(
                f"Management {path.split('/')[0]} request failed: HTTP {response.status_code}"
            )
        return response.json()

    def import_bundle(self, bundle, operation_key):
        accepted = self.request("POST", "imports/prepared-runs", bundle, operation_key + "-import")
        return self.wait("imports/" + accepted["import_id"], "status", {"completed", "failed"})

    def wait(self, path, key, states, *, timeout=300):
        deadline = time.monotonic() + timeout
        while True:
            try:
                response = self.client.get(path)
                if response.status_code in {502, 503, 504}:
                    raise httpx.ConnectError("Management is temporarily unavailable")
                if not response.is_success:
                    raise ValueError(f"Management observation failed: HTTP {response.status_code}")
                data = response.json()
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "Management is unreachable; the durable attempt was not restarted"
                    ) from None
                time.sleep(1)
                continue
            value = data
            for part in key.split("."):
                value = value[part]
            if value in states:
                return data
            if time.monotonic() >= deadline:
                raise ValueError(
                    "Management observation timed out; the durable attempt was not restarted"
                )
            time.sleep(1)

    def collect(self, view, directory):
        run = Run.model_validate(view["run"])
        atomic_json(directory / "management-run.json", view)
        artifacts = run.result.artifacts if run.result else {}
        registrations = {(r["uri"], r["sha256"]): r for r in self.request("GET", "artifacts")}
        captured = {}
        for name, artifact in artifacts.items():
            row = registrations.get((artifact.uri, artifact.sha256))
            if not row or row["status"] != "verified":
                raise ValueError(f"Management has not verified result artifact: {name}")
            # Names come from a shared dictionary; never treat them as destination paths.
            filename = hashlib.sha256(name.encode()).hexdigest()[:24] + ".artifact"
            destination = directory / "downloads" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".partial")
            sha = hashlib.sha256()
            try:
                with self.client.stream("GET", "artifacts/" + row["id"]) as response:
                    if not response.is_success:
                        raise ValueError(f"Management artifact download failed: {name}")
                    with temporary.open("wb") as stream:
                        for chunk in response.iter_bytes():
                            sha.update(chunk)
                            stream.write(chunk)
                if sha.hexdigest() != artifact.sha256:
                    raise ValueError(f"Downloaded artifact checksum differs: {name}")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            captured[name] = {
                "path": str(destination),
                "uri": artifact.uri,
                "sha256": artifact.sha256,
            }
        atomic_json(
            directory / "evidence.json",
            {
                "run_id": run.id,
                "fixture": run.fixture,
                "status": run.status,
                "snapshot_sha256": hashlib.sha256(canonical(run.snapshot)).hexdigest(),
                "validity": run.result.validity if run.result else None,
                "slo_verdict": run.result.slo_verdict if run.result else None,
                "timeline_origin_at": view.get("timeline_origin_at"),
                "artifacts": captured,
            },
        )
        return run


def submit_bundle(bundle_path, url, password_file, groups, operation_key, output):
    if not 8 <= len(operation_key) <= 180:
        raise ValueError("operation key must contain 8–180 characters")
    bundle = json.loads(Path(bundle_path).read_text())
    snapshot = RunSnapshot.model_validate(bundle["document"]["snapshot"])
    fixture = bundle["document"]["fixture"]
    if snapshot.target is None or fixture != (snapshot.target.mode == "local-stub"):
        raise ValueError("bundle fixture identity disagrees with execution target")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = {
        "operation_key": operation_key,
        "snapshot_sha256": hashlib.sha256(canonical(snapshot)).hexdigest(),
        "groups": groups,
        "management_url": url,
    }
    identity_path = output / "submission.json"
    if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
        raise ValueError("output already belongs to a different immutable submission")
    atomic_json(identity_path, identity)
    client = ManagementClient(url, password_file)
    try:
        receipt = client.import_bundle(bundle, operation_key)
        atomic_json(output / "import-receipt.json", receipt)
        if receipt["status"] != "completed":
            raise ValueError("Prepared bundle import failed; inspect the retained receipt")
        accepted = client.request(
            "POST",
            "runs",
            {
                "scenario_ref": snapshot.scenario.ref,
                "prepared_run_id": bundle["document"]["id"],
                "mode": "simulation" if fixture else "real",
                "group_ids": groups,
            },
            operation_key,
        )
        identity = accepted["resource_id"]
        atomic_json(output / "launch.json", accepted)
        budget = (
            snapshot.scenario.warmup.duration_s
            + snapshot.scenario.measurement_duration_s
            + snapshot.scenario.drain_timeout_s
            + 600
        )
        try:
            view = client.wait(
                "runs/" + identity,
                "run.status",
                {"completed", "failed", "cancelled"},
                timeout=budget,
            )
        except KeyboardInterrupt:
            client.request("POST", "runs/" + identity + "/stop", key=operation_key + "-stop")
            view = client.wait(
                "runs/" + identity, "run.status", {"completed", "failed", "cancelled"}, timeout=120
            )
        if RunSnapshot.model_validate(view["run"]["snapshot"]) != snapshot:
            raise ValueError("Management execution snapshot differs from the supplied bundle")
        return client.collect(view, output)
    finally:
        client.close()
