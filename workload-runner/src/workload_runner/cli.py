"""Dataset preparation, immutable planning, execution and historical reassessment."""

import argparse
import json
from pathlib import Path

import httpx
from platform_contracts import (
    ExecutionTarget,
    InfrastructureInventory,
    PreparationManifest,
    RunSnapshot,
    ServingBinding,
    load_catalog,
)
from platform_contracts.runs import ExecutionVersions

from .control import LocalRunnerAdapter, execute, initialize_attempt, reassess
from .datasets.preparation import prepare_customer
from .scenario_loader import compile_run


def execution_catalog(args):
    from platform_contracts import validate_catalog
    from platform_contracts.loading import CONFIG_MODELS, load_document

    catalog = load_catalog(args.repo)
    for item in args.config:
        kind, filename = item.split("=", 1)
        catalog.add(kind, load_document(Path(filename), CONFIG_MODELS[kind]))
    validate_catalog(catalog)
    return catalog


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--repo", type=Path, default=Path.cwd())
    root.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="KIND=YAML",
        help="add an immutable versioned deployment configuration",
    )
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
        "--serving-profile", help="exact serving profile reference for preparation"
    )
    prepare.add_argument("--tokenizer-revision", help="immutable tokenizer source revision")
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
    plan.add_argument(
        "--sustained", action="store_true", help="derive 20 scored minutes without changing targets"
    )
    plan.add_argument(
        "--serving-stack", choices=["direct-vllm", "llm-d-vllm"], default="llm-d-vllm"
    )
    plan.add_argument("--bindings", type=Path, help="JSON array of exact serving bindings")
    plan.add_argument("--inventory", type=Path, help="B's full inventory JSON")
    plan.add_argument("--versions", type=Path, help="JSON execution version/image manifest")
    plan.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--snapshot", type=Path)
    run.add_argument("--output", type=Path)
    run.add_argument("--attempt", type=Path, help=argparse.SUPPRESS)
    managed = commands.add_parser("execute")
    managed.add_argument("--manifest", type=Path, required=True)
    managed.add_argument("--credential-file", action="append", default=[], metavar="ENV=PATH")
    submit = commands.add_parser(
        "submit", help="import, launch and collect a frozen bundle through management"
    )
    submit.add_argument("--bundle", type=Path, required=True)
    submit.add_argument("--management-url", required=True)
    submit.add_argument("--password-file", type=Path, required=True)
    submit.add_argument(
        "--groups", type=Path, help="JSON map of frozen worker-group refs to runtime IDs"
    )
    submit.add_argument("--operation-key", required=True)
    submit.add_argument("--output", type=Path, required=True)
    submit.add_argument("--require-valid", action="store_true")
    for name in ("status", "stop", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("attempt", type=Path)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            catalog = execution_catalog(args)
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
                    serving_profile_ref=args.serving_profile,
                    tokenizer_revision=args.tokenizer_revision,
                )
                print(
                    f"{manifest.id}: {manifest.entry_count} inputs; "
                    f"sha256={manifest.artifact.sha256}"
                )
        elif args.command == "plan":
            catalog = execution_catalog(args)
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
                serving_stack=args.serving_stack,
                endpoint=args.endpoint,
                credential_env=dict(item.split("=", 1) for item in args.credential_env),
                stub_preset=args.preset,
            )
            snapshot = compile_run(
                catalog,
                ref,
                preparations,
                target,
                args.output,
                smoke=args.smoke,
                sustained=args.sustained,
                bindings=tuple(
                    ServingBinding.model_validate(b) for b in json.loads(args.bindings.read_text())
                )
                if args.bindings
                else None,
                inventory=InfrastructureInventory.model_validate_json(args.inventory.read_text())
                if args.inventory
                else None,
                versions=ExecutionVersions.model_validate_json(args.versions.read_text())
                if args.versions
                else None,
            )
            print(f"{snapshot.scenario.ref}: {args.output / 'snapshot.json'}")
        elif args.command == "submit":
            from .management import submit_bundle

            run = submit_bundle(
                args.bundle,
                args.management_url,
                args.password_file,
                json.loads(args.groups.read_text()) if args.groups else {},
                args.operation_key,
                args.output,
            )
            print(f"{run.id}: {run.status}; evidence: {args.output / 'evidence.json'}")
            return (
                0
                if run.status == "completed"
                and (not args.require_valid or run.result and run.result.validity == "valid")
                else 2
            )
        elif args.command == "execute":
            from .managed import execute_manifest

            run = execute_manifest(args.manifest, args.credential_file)
            return 0 if run.status == "completed" else 2
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
    except (ValueError, OSError, KeyError, httpx.HTTPError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
