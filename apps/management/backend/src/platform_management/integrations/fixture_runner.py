"""CPU-only contract-test runner. Never a measurement or quality evaluator."""

import argparse
import json
import os
import time
from pathlib import Path

from platform_contracts import RunSnapshot

from platform_management.integrations.runner_bridge import atomic
from platform_management.services.synthetic import result_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--outcome", default="passing")
    parser.add_argument("--delay", type=float, default=8)
    args = parser.parse_args()
    manifest = json.loads(Path(os.environ["KLIP_EXECUTION_MANIFEST"]).read_text())
    if not manifest["fixture"]:
        raise SystemExit("Fixture runner refuses non-fixture execution")
    root = Path(args.output)
    # Count actual subprocess invocations for integration duplicate-start assertions.
    with (root / "fixture-invocations.log").open("a") as stream:
        stream.write("synthetic invocation\n")
    snapshot = RunSnapshot.model_validate_json(Path(args.snapshot).read_text())
    for stage in ("preparing", "warming_up", "measuring", "draining", "finalizing"):
        atomic(root / "observation.json", {"status": "running", "stage": stage})
        time.sleep(args.delay / 5)
    if args.outcome == "failed":
        raise SystemExit(1)
    atomic(root / "result.json", result_for(snapshot, args.outcome).model_dump(mode="json"))


if __name__ == "__main__":
    main()
