"""Dataset preparation, immutable planning, execution and historical reassessment."""

import argparse
import json
from pathlib import Path

from platform_contracts import ExecutionTarget, PreparationManifest, RunSnapshot, load_catalog

from .control import LocalRunnerAdapter, execute, initialize_attempt, reassess
from .datasets.preparation import prepare_customer
from .scenario_loader import compile_run


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--repo", type=Path, default=Path.cwd())
    commands = root.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument(
        "--customer", default="all", help="customer ID, versioned reference, or all"
    )
    prepare.add_argument("--source", choices=["synthetic", "public"], default="synthetic")
    prepare.add_argument("--output", type=Path, default=Path("datasets/prepared"))
    prepare.add_argument("--limit", type=int)
    prepare.add_argument("--byte-limit", type=int, default=1024**3)
    prepare.add_argument(
        "--max-turns", type=int, help="contiguous session prefix bound; defaults to 8 for CodeAgent"
    )
    prepare.add_argument(
        "--tokenizer", help="override model tokenizer repository; revision is resolved and recorded"
    )
    plan = commands.add_parser("plan")
    plan.add_argument("--scenario", required=True)
    plan.add_argument("--prepared", type=Path, default=Path("datasets/prepared"))
    plan.add_argument("--source", choices=["synthetic", "public"], default="synthetic")
    plan.add_argument("--mode", choices=["local-stub", "benchmark"], default="local-stub")
    plan.add_argument("--endpoint", default="http://127.0.0.1:8001/v1/chat/completions")
    plan.add_argument(
        "--credential-env", action="append", default=[], metavar="CUSTOMER_ID=ENV_NAME"
    )
    plan.add_argument("--preset", default="healthy")
    plan.add_argument("--smoke", action="store_true")
    plan.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--snapshot", type=Path)
    run.add_argument("--output", type=Path)
    run.add_argument("--attempt", type=Path, help=argparse.SUPPRESS)
    for name in ("status", "stop", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("attempt", type=Path)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            catalog = load_catalog(args.repo)
            refs = (
                list(catalog.records["customer"])
                if args.customer == "all"
                else [args.customer if "@" in args.customer else args.customer + "@1"]
            )
            for ref in refs:
                manifest = prepare_customer(
                    catalog,
                    ref,
                    args.output,
                    source=args.source,
                    limit=args.limit,
                    byte_limit=args.byte_limit,
                    tokenizer=args.tokenizer,
                    max_turns=args.max_turns,
                )
                print(
                    f"{manifest.id}: {manifest.entry_count} inputs; "
                    f"sha256={manifest.artifact.sha256}"
                )
        elif args.command == "plan":
            catalog = load_catalog(args.repo)
            ref = args.scenario if "@" in args.scenario else args.scenario + "@1"
            preparations = {}
            for participant in catalog.get("scenario", ref).participants:
                customer = catalog.get("customer", participant)
                pointer = args.prepared / args.source / customer.id / "latest.json"
                preparations[participant] = PreparationManifest.model_validate_json(
                    pointer.read_text()
                )
            target = ExecutionTarget(
                mode=args.mode,
                endpoint=args.endpoint,
                credential_env=dict(item.split("=", 1) for item in args.credential_env),
                stub_preset=args.preset,
            )
            snapshot = compile_run(
                catalog, ref, preparations, target, args.output, smoke=args.smoke
            )
            print(f"{snapshot.scenario.ref}: {args.output / 'snapshot.json'}")
        elif args.command == "run":
            if args.attempt:
                directory = args.attempt
            else:
                if not args.snapshot or not args.output:
                    raise ValueError("run requires --snapshot and --output")
                directory = args.output
                if (directory / "run.json").exists():
                    raise ValueError(
                        "execution output already exists; use status or a new attempt directory"
                    )
                snapshot = RunSnapshot.model_validate_json(args.snapshot.read_text())
                initialize_attempt(snapshot, directory)
            run = execute(directory)
            print(f"{run.id}: {run.status}; {directory / 'report.md'}")
            return 0 if run.status == "completed" else 2
        elif args.command in {"status", "stop"}:
            directory = args.attempt.resolve()
            adapter = LocalRunnerAdapter(directory.parent)
            if args.command == "stop":
                adapter.stop(str(directory))
            print(adapter.observe(str(directory)).model_dump_json(indent=2))
        elif args.command == "evaluate":
            print(reassess(args.attempt.resolve()))
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
