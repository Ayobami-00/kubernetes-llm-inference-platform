"""Actual AIPerf runner Jobs through C; disposable kind cluster created by dev/test-integration."""

import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "management_proof", ROOT / "scripts/management/integration_smoke.py"
)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
SCENARIOS = [
    "writer-assist-baseline",
    "tutor-chat-baseline",
    "doc-flow-baseline",
    "research-agent-baseline",
    "code-agent-baseline",
    "shared-contention",
    "platform-mixed",
]


def kubectl(*args):
    return json.loads(proof.run("kubectl", "-n", "klip", *args, "-o", "json"))


def job(run_id):
    jobs = kubectl("get", "jobs", "-l", "klip.dev/run-id=" + run_id)["items"]
    assert len(jobs) <= 1, "One operation must never create multiple Jobs"
    return jobs[0] if jobs else None


def main(root, campaign="initial"):
    root = root.resolve()
    if not proof.run(
        "kubectl",
        "-n",
        "klip",
        "get",
        "deployment/inference-stub",
        "--ignore-not-found",
        "-o",
        "name",
    ).strip():
        proof.run(
            "kubectl",
            "-n",
            "klip",
            "create",
            "deployment",
            "inference-stub",
            "--image=klip-integration-stub:local",
        )
    if not proof.run(
        "kubectl", "-n", "klip", "get", "service/inference-stub", "--ignore-not-found", "-o", "name"
    ).strip():
        proof.run(
            "kubectl",
            "-n",
            "klip",
            "expose",
            "deployment",
            "inference-stub",
            "--port=8000",
            "--target-port=8001",
        )
    proof.run(
        "kubectl", "-n", "klip", "rollout", "status", "deployment/inference-stub", "--timeout=120s"
    )
    values = json.loads((root / "values.json").read_text())
    values["runner"].update(
        simulationAdapter="kubernetes-workload-stub:v1",
        image="klip-integration-workloads:local",
        command=["workload-runner", "execute", "--manifest", "{attempt_dir}/manifest.json"],
        resources={
            "requests": {"cpu": "1", "memory": "512Mi"},
            "limits": {"cpu": "4", "memory": "2Gi"},
        },
    )
    path = root / "workload-values.json"
    path.write_text(json.dumps(values))
    proof.run(
        "helm",
        "upgrade",
        "klip",
        "charts/klip",
        "-n",
        "klip",
        "-f",
        str(path),
        "--wait",
        "--timeout=180s",
    )
    artifact_root = root / "data/artifacts"

    def runner(*args):
        proof.run(
            "docker",
            "run",
            "--rm",
            "-v",
            f"{artifact_root}:/artifacts",
            "klip-integration-workloads:local",
            *args,
        )

    runner("prepare", "--source", "synthetic", "--limit", "100", "--output", "/artifacts/prepared")
    api = proof.API(root)
    results = []
    for scenario in SCENARIOS:
        runner(
            "plan",
            "--scenario",
            scenario,
            "--prepared",
            "/artifacts/prepared",
            "--smoke",
            "--endpoint",
            "http://inference-stub.klip:8000/v1/chat/completions",
            "--output",
            "/artifacts/plans/" + scenario,
        )
        snapshot = json.loads((artifact_root / "plans" / scenario / "snapshot.json").read_text())
        identity = "actual-a-" + scenario
        api.import_document("prepared-runs", dict(id=identity, fixture=True, snapshot=snapshot))
        body = dict(
            scenario_ref=snapshot["scenario"]["id"] + "@1",
            prepared_run_id=identity,
            mode="simulation",
        )
        readiness = api.request("POST", "/runs/preflight", body)
        assert readiness["simulation_available"] and not readiness["executable"]
        key = "workload-kind-" + campaign + "-" + scenario
        accepted = api.request("POST", "/runs", body, key)
        assert api.request("POST", "/runs", body, key) == accepted
        run_id = accepted["resource_id"]
        deadline = time.monotonic() + 60
        while not job(run_id):
            assert time.monotonic() < deadline, "Runner Job was not created"
            time.sleep(0.2)
        uid = job(run_id)["metadata"]["uid"]
        if scenario == "writer-assist-baseline":
            deadline = time.monotonic() + 90
            while not list((artifact_root / "runs" / run_id).glob("*/engine-clock.json")):
                assert time.monotonic() < deadline, "Runner did not start its common timeline"
                time.sleep(0.1)
            proof.run("kubectl", "-n", "klip", "rollout", "restart", "deployment/klip-worker")
            proof.run(
                "kubectl",
                "-n",
                "klip",
                "rollout",
                "status",
                "deployment/klip-worker",
                "--timeout=120s",
            )
        completed = api.wait("/runs/" + run_id, "run.status", "completed", timeout=180)
        assert job(run_id)["metadata"]["uid"] == uid
        assert completed["run"]["snapshot"] == snapshot
        assert (
            completed["run"]["fixture"] and completed["run"]["result"]["requests"]["completed"] > 0
        )
        result = completed["run"]["result"]
        if result["validity"] == "invalid":
            # A shared CPU host can miss the strict delivery target. Preserve that outcome;
            # it must never become a passing SLO result or be hidden by retries/looser policy.
            assert result["invalid_reasons"] == ["excessive_scheduling_lag"]
            assert result["slo_verdict"] == "not_evaluated" and not result["slo_assessments"]
        from workload_runner.management import ManagementClient

        collector = ManagementClient(api.url, root / "admin_password")
        try:
            collector.collect(completed, ROOT / "artifacts/workload-management-runs" / run_id)
        finally:
            collector.close()
        assert completed["timeline_origin_at"]
        report_uri = completed["run"]["result"]["artifacts"]["report"]["uri"]
        registered = next(a for a in api.request("GET", "/artifacts") if a["uri"] == report_uri)
        with api.opener.open(api.url + "/api/v1/artifacts/" + registered["id"]) as response:
            content = response.read()
        assert b"SYNTHETIC / EMULATED" in content
        assert hashlib.sha256(content).hexdigest() == registered["sha256"]
        results.append(
            dict(
                scenario=scenario,
                run_id=run_id,
                job_uid=uid,
                validity=completed["run"]["result"]["validity"],
                resources=job(run_id)["spec"]["template"]["spec"]["containers"][0]["resources"],
            )
        )
        print(f"Actual A runner completed through C: {scenario}", flush=True)
    cli_command = [
        sys.executable,
        "-m",
        "workload_runner.cli",
        "submit",
        "--bundle",
        str(artifact_root / "plans/writer-assist-baseline/management-bundle.json"),
        "--management-url",
        api.url,
        "--password-file",
        str(root / "admin_password"),
        "--operation-key",
        "workload-kind-" + campaign + "-cli-submit",
        "--output",
        str(ROOT / "artifacts/workload-managed-cli" / root.name / campaign),
    ]
    proof.run(*cli_command)
    # Re-observe the same terminal attempt with the strict CLI gate. A delivery-invalid result
    # must return 2 rather than being relabelled as a successful benchmark on a noisy CPU host.
    strict = subprocess.run([*cli_command, "--require-valid"], capture_output=True, text=True)
    evidence_path = ROOT / "artifacts/workload-managed-cli" / root.name / campaign / "evidence.json"
    cli_evidence = json.loads(evidence_path.read_text())
    assert strict.returncode == (0 if cli_evidence["validity"] == "valid" else 2), strict.stdout
    # Cancellation uses a different immutable plan and operation; never modify the earlier snapshot.
    runner(
        "plan",
        "--scenario",
        "platform-mixed",
        "--prepared",
        "/artifacts/prepared",
        "--smoke",
        "--preset",
        "timeout",
        "--endpoint",
        "http://inference-stub.klip:8000/v1/chat/completions",
        "--output",
        "/artifacts/plans/cancellation",
    )
    snapshot = json.loads((artifact_root / "plans/cancellation/snapshot.json").read_text())
    api.import_document(
        "prepared-runs", dict(id="actual-a-cancel", fixture=True, snapshot=snapshot)
    )
    accepted = api.request(
        "POST",
        "/runs",
        dict(
            scenario_ref=snapshot["scenario"]["id"] + "@1",
            prepared_run_id="actual-a-cancel",
            mode="simulation",
        ),
    )
    api.wait("/runs/" + accepted["resource_id"], "run.status", "running")
    api.request("POST", "/runs/" + accepted["resource_id"] + "/stop")
    api.wait("/runs/" + accepted["resource_id"], "run.status", "cancelled", timeout=180)
    evidence = dict(
        fixture=True,
        source="actual-aiperf-through-management-kind",
        scenarios=results,
        cancellation="cancelled",
    )
    output = ROOT / "artifacts/workload-management-acceptance.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    print("Actual A↔C Kubernetes integration passed; evidence: " + str(output), flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "initial")
