#!/usr/bin/env python3
"""CPU-only acceptance driver. Public preparation is opt-in and network dependent."""

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from platform_contracts import ExecutionTarget, PreparationManifest, load_catalog
from workload_runner.artifacts import atomic_json
from workload_runner.control import execute, initialize_attempt
from workload_runner.datasets.preparation import prepare_customer
from workload_runner.scenario_loader import compile_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["synthetic", "public"], default="synthetic")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--prepared", type=Path, default=Path("datasets/prepared"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    catalog = load_catalog(Path(__file__).resolve().parents[1])
    prepared = {}
    for ref, customer in catalog.records["customer"].items():
        if args.prepare:
            manifest = prepare_customer(catalog, ref, args.prepared, source=args.source, limit=100)
        else:
            manifest = PreparationManifest.model_validate_json(
                (args.prepared / args.source / customer.id / "latest.json").read_text()
            )
        prepared[ref] = manifest
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "stub.log").open("w") as log:
        stub = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "inference_stub.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout-graceful-shutdown",
                "2",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    summaries = []
    try:
        origin = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(origin + "/health", timeout=1, trust_env=False).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("local inference stub did not become ready")
        for ref, scenario in catalog.records["scenario"].items():
            if args.standalone and len(scenario.participants) != 1:
                continue
            directory = args.output / scenario.id
            snapshot = compile_run(
                catalog,
                ref,
                prepared,
                ExecutionTarget(mode="local-stub", endpoint=origin + "/v1/chat/completions"),
                directory / "plan",
                smoke=True,
            )
            initialize_attempt(snapshot, directory / "execution")
            run = execute(directory / "execution")
            summaries.append(
                dict(
                    scenario_ref=ref,
                    run_id=run.id,
                    status=run.status,
                    validity=run.result.validity if run.result else None,
                    slo_verdict=run.result.slo_verdict if run.result else None,
                    request_count=run.result.requests.offered if run.result else 0,
                )
            )
            print(summaries[-1], flush=True)
            if run.status != "completed":
                raise RuntimeError(f"{scenario.id}: {run.error}")
        atomic_json(
            args.output / "acceptance.json",
            dict(
                fixture=True,
                source=args.source,
                preparations=[
                    dict(
                        customer_ref=p.customer_ref,
                        id=p.id,
                        entries=p.entry_count,
                        sha256=p.artifact.sha256,
                    )
                    for p in prepared.values()
                ],
                runs=summaries,
            ),
        )
    finally:
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stub.kill()
            stub.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
