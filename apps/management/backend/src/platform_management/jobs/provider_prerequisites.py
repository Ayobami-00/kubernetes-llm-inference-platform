"""One CPU-only Job prepares free Lambda prerequisites; never rents an instance."""

import argparse
import hashlib
import json
from pathlib import Path

from platform_management.host_executor.client import HostExecutorClient
from platform_management.integrations.job_runner import frozen_file
from platform_management.providers.lambda_ledger import request_gate
from platform_management.providers.lambda_mutations import ProviderIdentityConflict
from platform_management.providers.lambda_prerequisites import (
    LambdaPrerequisites,
    PrerequisiteSpec,
    prepare_once,
)
from platform_management.providers.prerequisite_ledger import (
    PostgresPrerequisiteLedger,
    setup_owner,
)
from platform_management.settings import get_settings


def execute(spec, settings, *, verify_only=False):
    key = settings.secret(settings.lambda_api_key_file, required=True)
    if hashlib.sha256(key.encode()).hexdigest() != spec.provider_credential_sha256:
        raise ProviderIdentityConflict("Provider setup credential reference changed")
    client = HostExecutorClient(settings.host_executor_tls_home)
    try:
        metadata = client.cluster()
    finally:
        client.close()
    if (
        metadata.get("schema_version") != "klip-host-executor-metadata.v1"
        or metadata.get("kube_system_uid") != spec.kube_system_uid
        or metadata.get("bootstrap_public_key") != spec.public_key
        or sorted(metadata.get("ssh_source_cidrs", []))
        != sorted(str(v) for n, v in spec.cpu_public_ips.items() if n.startswith("platform-"))
    ):
        raise ProviderIdentityConflict(
            "Provider setup differs from the live executor's public identity"
        )
    ledger = PostgresPrerequisiteLedger()
    with setup_owner():
        if verify_only:
            result = ledger.read(spec)
            if result.get("state") != "ready":
                raise ProviderIdentityConflict("Provider prerequisites are not ready")
        api = LambdaPrerequisites(key, gate=request_gate)
        try:
            return prepare_once(spec, api, ledger)
        finally:
            api.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.spec.is_symlink() or args.spec.stat().st_size > 65536:
            raise ValueError("Unsafe provider setup input")
        spec = PrerequisiteSpec.model_validate_json(args.spec.read_bytes())
        result = execute(spec, get_settings(), verify_only=args.verify_only)
    except Exception as error:
        # API bodies, credentials and arbitrary exception messages never become logs.
        result = {
            "state": "reconciliation_required",
            "error_type": type(error).__name__,
            "gpu_capacity_provisioned": False,
        }
        frozen_file(args.output, json.dumps(result, sort_keys=True).encode())
        print(json.dumps(result))
        raise SystemExit(1) from None
    frozen_file(args.output, json.dumps(result, sort_keys=True).encode())
    print(
        json.dumps(
            {"setup_id": spec.identity, "state": result["state"], "gpu_capacity_provisioned": False}
        )
    )


if __name__ == "__main__":
    main()
