"""Plan/apply operator entrypoint. Saved-plan digests are mandatory for mutations."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from backend import initialize, migrate_bootstrap, repair_migration_metadata
from common import (
    CONFIG,
    PROFILES,
    ROOT,
    ROOTS,
    Aws,
    ensure_ssh_key,
    private_dir,
    source_digest,
    state_home,
    tf,
    utc_now,
    write_private,
)
from configuration import validate_configuration, validate_origins


def plan_paths(root: str, preview: bool = False) -> tuple[Path, Path]:
    folder = private_dir(state_home() / "plans")
    prefix = f"{root}-preview" if preview else root
    return folder / f"{prefix}.tfplan", folder / f"{prefix}.receipt.json"


def plan_changes(document: dict) -> list[dict]:
    return [
        {
            "address": item["address"],
            "actions": item["change"]["actions"],
            "importing": item["change"].get("importing"),
        }
        for item in document.get("resource_changes", [])
        if item.get("mode") != "data"
        and (item["change"]["actions"] != ["no-op"] or item["change"].get("importing"))
    ]


def input_values(root: str, origins_file: Path | None, deletion_protection: bool = True) -> dict:
    if root == "foundation":
        public, _ = ensure_ssh_key()
        return {
            "ssh_public_key": public,
            "deletion_protection": deletion_protection,
            "reserved_cidrs": [],
        }
    if root == "dns":
        if origins_file is None:
            raise ValueError(
                "DNS plans require --origins-file, including for an explicitly empty map"
            )
        values = json.loads(origins_file.read_text())
        if (
            not isinstance(values, dict)
            or set(values) - {"origins", "health_path"}
            or "origins" not in values
        ):
            raise ValueError("Origins file must contain origins and optionally health_path only")
        validate_origins(values["origins"])
        path = values.get("health_path", "/readyz")
        if not isinstance(path, str) or not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("Invalid gateway health-check path")
        values["health_path"] = path
        return values
    if root == "bootstrap":
        return {"adopt_existing_access": True}
    if root == "identity":
        from workload_identity import signing_input

        return {"signing_jwks": signing_input()}
    if root == "platform":
        from configuration import validate_platform_configuration

        platform = json.loads((ROOT / "infra/terraform/platform.json").read_text())
        validate_platform_configuration(CONFIG, platform)
        public, _ = ensure_ssh_key()
        return {"ssh_public_key": public, "deletion_protection": deletion_protection}
    if root == "probe":
        public, _ = ensure_ssh_key()
        return {"ssh_public_key": public}
    return {}


def create_plan(
    root: str,
    *,
    preview: bool = False,
    origins_file: Path | None = None,
    phase: str = "apply",
    teardown: dict | None = None,
    allow_origin_removal: bool = False,
) -> dict:
    if root == "bootstrap" and preview:
        raise ValueError("Bootstrap's local first-use plan is real; omit --preview")
    if root in {"identity", "storage", "storage-access", "registry"} and preview:
        raise ValueError(
            "Retained identity/storage uses a real administrator-owned saved plan; omit --preview"
        )
    if allow_origin_removal and root != "dns":
        raise ValueError("Origin removal applies only to the DNS root")
    validate_configuration(CONFIG)
    values = input_values(root, origins_file, deletion_protection=phase == "apply")
    initialize(root, preview=preview)
    profile = PROFILES["bootstrap"] if preview else PROFILES[root]
    identity = Aws(profile).preflight("bootstrap" if preview else root)
    plan_file, receipt_file = plan_paths(root, preview)
    values_file = plan_file.with_suffix(".variables.json")
    write_private(values_file, values)
    arguments = [
        "plan",
        "-input=false",
        "-lock-timeout=60s",
        f"-var-file={values_file}",
        f"-out={plan_file}",
    ]
    if phase == "destroy":
        arguments.append("-destroy")
    output = tf(root, *arguments, profile=profile)
    plan_file.chmod(0o600)
    write_private(plan_file.with_suffix(".txt"), output)
    document = json.loads(tf(root, "show", "-json", str(plan_file), profile=profile))
    write_private(plan_file.with_suffix(".json"), document)
    changes = plan_changes(document)
    deleting = [item for item in changes if "delete" in item["actions"]]
    if deleting and allow_origin_removal:
        for item in deleting:
            if not item["address"].startswith(
                (
                    "module.inference_dns.aws_route53_record.",
                    "module.inference_dns.aws_route53_health_check.",
                )
            ):
                raise ValueError("DNS origin removal cannot delete other resource types")
    if phase == "apply" and deleting and not allow_origin_removal:
        raise ValueError(
            "Normal apply would delete or replace resources; "
            "review a separate decommission/replacement procedure"
        )
    if phase == "unprotect":
        for item in document.get("resource_changes", []):
            if item.get("mode") == "data" or item["change"]["actions"] == ["no-op"]:
                continue
            allowed_field = {
                "aws_instance": "disable_api_termination",
                "aws_lb": "enable_deletion_protection",
            }.get(item["type"])
            before, after = item["change"]["before"], item["change"]["after"]
            if (
                not allowed_field
                or item["change"]["actions"] != ["update"]
                or any(
                    before.get(key) != after.get(key)
                    for key in set(before) | set(after)
                    if key != allowed_field
                )
            ):
                raise ValueError(
                    "The unprotect plan contains unrelated changes; resolve drift before teardown"
                )
    receipt = {
        "root": root,
        "preview": preview,
        "phase": phase,
        "created_at": utc_now(),
        "account_id": CONFIG["account_id"],
        "actor": identity["Arn"],
        "plan_path": str(plan_file),
        "variables_path": str(values_file),
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "variables_sha256": hashlib.sha256(values_file.read_bytes()).hexdigest(),
        "source_sha256": source_digest(),
        "changes": changes,
        "teardown": teardown,
        "allow_origin_removal": allow_origin_removal,
    }
    write_private(receipt_file, receipt)
    print(output)
    print(
        json.dumps(
            {
                "receipt": str(receipt_file),
                "approve_plan": receipt["plan_sha256"],
                "preview_only": preview,
                "changes": changes,
            },
            indent=2,
        )
    )
    return receipt


def verify_receipt(receipt: dict, approval: str, current_source: str, root: str) -> None:
    if receipt["root"] != root or receipt.get("applied_at") or receipt.get("terraform_applied_at"):
        raise ValueError("Plan root differs or this plan was already applied; generate a new plan")
    if receipt["preview"]:
        raise ValueError(
            "Preview plans cannot be applied; create a fresh S3-backed plan after bootstrap"
        )
    if approval != receipt["plan_sha256"]:
        raise ValueError("Approval must identify the exact saved plan SHA-256")
    if receipt["account_id"] != CONFIG["account_id"] or receipt["source_sha256"] != current_source:
        raise ValueError("Account or implementation changed after planning; generate a new plan")
    for path_key, hash_key in (
        ("plan_path", "plan_sha256"),
        ("variables_path", "variables_sha256"),
    ):
        if hashlib.sha256(Path(receipt[path_key]).read_bytes()).hexdigest() != receipt[hash_key]:
            raise ValueError("Saved plan or its inputs changed after review")


def apply_plan(root: str, approval: str, *, teardown: bool = False) -> None:
    _, receipt_file = plan_paths(root)
    receipt = json.loads(receipt_file.read_text())
    verify_receipt(receipt, approval, source_digest(), root)
    if (receipt["phase"] in {"unprotect", "destroy"}) != teardown:
        raise ValueError("Use the explicit destroy command for teardown plans")
    Aws(PROFILES[root]).preflight(root)
    if root == "identity" and receipt["phase"] == "apply":
        from workload_identity import verify_signers_for_apply

        verify_signers_for_apply(
            json.loads(Path(receipt["variables_path"]).read_text())["signing_jwks"]
        )
    if root == "platform" and receipt["phase"] == "apply":
        from platform_capacity import ensure_capacity

        document = json.loads(tf(root, "show", "-json", receipt["plan_path"]))
        evidence = ensure_capacity(Aws(PROFILES[root]), document)
        write_private(Path(receipt["plan_path"]).with_suffix(".capacity.json"), evidence)
    if teardown:
        if root == "probe":
            verify_probe_teardown(receipt["teardown"])
        else:
            verify_teardown_evidence(receipt["teardown"])
        if root != "probe" and receipt["teardown"]["empty_etcd"]:
            from smoke import assert_prebootstrap, ssm_command

            aws = Aws(PROFILES["foundation"])
            for node in allocation()["nodes"].values():
                ssm_command(aws, node["instance_id"], assert_prebootstrap(include_etcd=True))
    try:
        result = tf(root, "apply", "-input=false", str(receipt["plan_path"]))
    except RuntimeError as error:
        write_private(state_home() / f"reports/{root}-apply-error.txt", str(error))
        raise
    write_private(Path(receipt["plan_path"]).with_suffix(".apply.txt"), result)
    receipt["terraform_applied_at"] = utc_now()
    write_private(receipt_file, receipt)
    print(result)
    if root == "bootstrap":
        migrate_bootstrap()
    receipt["applied_at"] = utc_now()
    write_private(receipt_file, receipt)


def verify_probe_teardown(evidence: dict) -> None:
    if not evidence or evidence.get("cluster_id") != CONFIG["cluster_id"]:
        raise ValueError("Explicit probe cleanup evidence is required")
    outputs = json.loads(tf("probe", "output", "-json"))
    if "probe" in outputs:
        current = outputs["probe"]["value"]
        if current["instance_id"] != evidence.get("probe_instance_id"):
            raise ValueError("Probe teardown targets a different instance")
    else:
        # A partial destroy may remove compute and outputs before releasing its
        # address. Permit only the exact recorded, tagged residual allocation.
        resources = (
            json.loads(tf("probe", "show", "-json"))
            .get("values", {})
            .get("root_module", {})
            .get("resources", [])
        )
        expected_tags = {
            "Project": CONFIG["project"],
            "Cluster": CONFIG["cluster_id"],
            "ManagedBy": "terraform-network-probe",
            "Node": "network-probe",
        }
        if len(resources) != 1 or resources[0]["address"] != "aws_eip.probe":
            raise ValueError("Missing probe output; inspect partial teardown ownership")
        values = resources[0]["values"]
        if (
            not evidence.get("probe_allocation_id")
            or values["id"] != evidence["probe_allocation_id"]
            or any(values.get("tags", {}).get(key) != value for key, value in expected_tags.items())
        ):
            raise ValueError("Partial teardown does not match the recorded unassociated probe EIP")
        observed = Aws(PROFILES["foundation"]).call(
            "ec2", "describe-addresses", "--allocation-ids", evidence["probe_allocation_id"]
        )["Addresses"]
        if (
            len(observed) != 1
            or observed[0].get("AssociationId")
            or any(
                {tag["Key"]: tag["Value"] for tag in observed[0].get("Tags", [])}.get(key) != value
                for key, value in expected_tags.items()
            )
        ):
            raise ValueError("Residual probe EIP is still associated or its live ownership differs")
    if not evidence.get("node_absent") or not evidence.get("peers_revoked"):
        raise ValueError("Remove probe Kubernetes membership and peers before teardown")
    from cluster import FOLDER, kubectl

    if "network-probe" in json.loads((FOLDER / "peers.json").read_text())["peers"]:
        raise ValueError("Probe still appears in the current mesh registry")
    if kubectl("get", "node", "network-probe", "--ignore-not-found", "-o", "json").strip():
        raise ValueError("Probe still appears in Kubernetes")


def allocation() -> dict:
    Aws(PROFILES["foundation"]).preflight("foundation")
    return json.loads(tf("foundation", "output", "-json", "foundation_manifest"))


def teardown_evidence(
    cluster: str | None, empty: bool, backup: Path | None, backup_hash: str | None, verified: bool
) -> dict:
    if cluster != CONFIG["cluster_id"]:
        raise ValueError("Explicit --confirm-cluster klip is required")
    if empty:
        if backup is not None:
            raise ValueError("Choose either an empty-etcd acknowledgement or a verified backup")
        return {"cluster_id": cluster, "empty_etcd": True}
    if not backup or not backup_hash or not verified:
        raise ValueError(
            "Supply --backup-file, --backup-sha256 and --confirm-backup-verified, "
            "or --ack-empty-etcd"
        )
    if not backup.is_file() or backup.stat().st_size == 0:
        raise ValueError(
            "The backup must be an existing nonempty file outside the destroyed infrastructure"
        )
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    if digest != backup_hash:
        raise ValueError("Backup checksum does not match")
    return {
        "cluster_id": cluster,
        "empty_etcd": False,
        "backup_ref": backup.resolve().as_uri(),
        "sha256": digest,
        "operator_verified_etcd_snapshot": True,
    }


def verify_teardown_evidence(evidence: dict) -> None:
    if not evidence or evidence["cluster_id"] != CONFIG["cluster_id"]:
        raise ValueError("Missing teardown evidence")
    if not evidence["empty_etcd"]:
        from urllib.parse import unquote, urlparse

        path = Path(unquote(urlparse(evidence["backup_ref"]).path))
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != evidence["sha256"]
        ):
            raise ValueError("Verified backup is no longer available unchanged")


def destroy_plan(args: argparse.Namespace) -> None:
    if args.root != "foundation":
        raise ValueError(
            "destroy-plan supports the foundation root only; "
            "use the guarded acceptance cleanup workflow for probes "
            "and retain backup/bootstrap roots"
        )
    from smoke import assert_prebootstrap, ssm_command

    manifest = allocation()
    evidence = teardown_evidence(
        args.confirm_cluster,
        args.ack_empty_etcd,
        args.backup_file,
        args.backup_sha256,
        args.confirm_backup_verified,
    )
    dns = Aws(PROFILES["dns"])
    dns.preflight("dns")
    records = dns.call(
        "route53", "list-resource-record-sets", "--hosted-zone-id", CONFIG["hosted_zone_id"]
    )
    if any(
        record["Name"].rstrip(".") == CONFIG["inference_hostname"] and record["Type"] == "A"
        for record in records["ResourceRecordSets"]
    ):
        raise ValueError(
            "Remove active inference origins with the DNS workflow before decommissioning"
        )
    if args.ack_empty_etcd:
        aws = Aws(PROFILES["foundation"])
        for node in manifest["nodes"].values():
            ssm_command(aws, node["instance_id"], assert_prebootstrap(include_etcd=True))
    phase = "unprotect" if manifest.get("deletion_protection", True) else "destroy"
    create_plan("foundation", phase=phase, teardown=evidence)
    print(
        "This plan disables protection only; plan teardown again after applying it."
        if phase == "unprotect"
        else "This plan deletes CPU hosts, networking, the NLB and all three etcd disks."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "init",
            "plan",
            "apply",
            "smoke",
            "export",
            "verify",
            "destroy-plan",
            "destroy",
            "profiles",
            "migrate-bootstrap",
            "repair-migration-metadata",
        ],
    )
    parser.add_argument("--root", choices=list(ROOTS), default="foundation")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--origins-file", type=Path)
    parser.add_argument("--allow-origin-removal", action="store_true")
    parser.add_argument("--approve-plan")
    parser.add_argument("--confirm-prebootstrap", action="store_true")
    parser.add_argument("--confirm-cluster")
    parser.add_argument("--ack-empty-etcd", action="store_true")
    parser.add_argument("--backup-file", type=Path)
    parser.add_argument("--backup-sha256")
    parser.add_argument("--confirm-backup-verified", action="store_true")
    parser.add_argument("--ca-bundle", type=Path)
    parser.add_argument("--bootstrap-observations", type=Path)
    parser.add_argument("--expected-remote-version")
    parser.add_argument("--expected-local-lineage")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.root, preview=args.preview)
    elif args.command == "migrate-bootstrap":
        migrate_bootstrap()
    elif args.command == "repair-migration-metadata":
        repair_migration_metadata(args.expected_remote_version, args.expected_local_lineage)
    elif args.command == "plan":
        create_plan(
            args.root,
            preview=args.preview,
            origins_file=args.origins_file,
            allow_origin_removal=args.allow_origin_removal,
        )
    elif args.command in {"apply", "destroy"}:
        if not args.approve_plan:
            raise ValueError("A reviewed --approve-plan SHA-256 is required")
        apply_plan(args.root, args.approve_plan, teardown=args.command == "destroy")
    elif args.command == "destroy-plan":
        destroy_plan(args)
    elif args.command == "smoke":
        from smoke import smoke

        if not args.confirm_prebootstrap:
            raise ValueError(
                "Smoke testing stops cp-03; explicit --confirm-prebootstrap is required"
            )
        smoke(allocation())
    elif args.command in {"export", "verify"}:
        from inventory import export_inventory

        export_inventory(
            allocation(),
            ca_bundle=args.ca_bundle,
            bootstrap_observations=args.bootstrap_observations,
        )
    else:
        from profiles import configure_profiles

        configure_profiles()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None
