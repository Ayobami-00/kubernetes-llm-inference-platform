"""Temporary, durable ALB target draining for one Terraform-owned CPU worker."""

from __future__ import annotations

from common import CONFIG, PROFILES, Aws, utc_now
from management_frontend import binding, check_targets, manifest
from smoke import wait_until


class TargetsNotReady(ValueError):
    """Exact, owned targets exist but health checks have not recovered yet."""


def prepare():
    from backend import initialize

    initialize("frontend")
    value = manifest()
    check_targets(value, Aws(PROFILES["platform"]))
    return binding(value)


def observed(record):
    declared = record["frontend"]
    if (
        binding(manifest()) != declared
        or declared["nodes"].get(record["node"]) != record["instance_id"]
    ):
        raise ValueError("Frontend allocation or selected target changed during maintenance")
    aws = Aws(PROFILES["platform"])
    tags = aws.call("elbv2", "describe-tags", "--resource-arns", declared["target_group_arn"])[
        "TagDescriptions"
    ]
    expected = {
        "Project": CONFIG["project"],
        "Cluster": CONFIG["cluster_id"],
        "ManagedBy": "terraform-platform-frontend",
    }
    if len(tags) != 1 or any(
        {t["Key"]: t["Value"] for t in tags[0]["Tags"]}.get(k) != v for k, v in expected.items()
    ):
        raise ValueError("Frontend ownership tags changed")
    targets = aws.call(
        "elbv2", "describe-target-health", "--target-group-arn", declared["target_group_arn"]
    )["TargetHealthDescriptions"]
    return validate_targets(targets, declared["nodes"], record["instance_id"])


def validate_targets(targets, nodes, selected):
    if len(nodes) != 3 or selected not in nodes.values():
        raise ValueError("Maintenance must select one of the three owned frontend targets")
    ids = [t["Target"]["Id"] for t in targets]
    if (
        len(set(ids)) != len(ids)
        or not set(ids) <= set(nodes.values())
        or any(t["Target"].get("Port") != 30443 for t in targets)
    ):
        raise ValueError("Unexpected target identity/port in maintenance group")
    survivors = set(nodes.values()) - {selected}
    healthy = {t["Target"]["Id"] for t in targets if t["TargetHealth"]["State"] == "healthy"}
    if not survivors <= healthy:
        raise TargetsNotReady("Both other management targets must remain healthy")
    return {t["Target"]["Id"]: t["TargetHealth"]["State"] for t in targets}


def wait_for_survivors(record):
    # Kubernetes readiness can precede ALB health-check convergence after a
    # reschedule. Retry only that observation; identity/ownership errors and
    # mutations are never retried here, and EC2 remains unchanged until healthy.
    def probe():
        try:
            return observed(record)
        except TargetsNotReady:
            return None

    return wait_until(probe, "both surviving ALB targets healthy", timeout=180, interval=5)


def require_detached(record):
    if record["instance_id"] in observed(record):
        raise ValueError("Selected management target must finish draining before restart")


def detach(record):
    from platform_resize import save

    states = wait_for_survivors(record)
    if record["instance_id"] not in states:
        require_detached(record)
        return
    record["frontend_detach_intent_at"] = utc_now()
    save(record)
    Aws(PROFILES["platform"]).call(
        "elbv2",
        "deregister-targets",
        "--target-group-arn",
        record["frontend"]["target_group_arn"],
        "--targets",
        f"Id={record['instance_id']},Port=30443",
    )
    wait_until(
        lambda: record["instance_id"] not in observed(record),
        "selected ALB target connection drain",
        timeout=120,
        interval=5,
    )
    record["frontend_detached_at"] = utc_now()
    save(record)


def restore(record):
    from platform_resize import save

    states = wait_for_survivors(record)
    if states.get(record["instance_id"]) != "healthy":
        record["frontend_restore_intent_at"] = utc_now()
        save(record)
        Aws(PROFILES["platform"]).call(
            "elbv2",
            "register-targets",
            "--target-group-arn",
            record["frontend"]["target_group_arn"],
            "--targets",
            f"Id={record['instance_id']},Port=30443",
        )
        wait_until(
            lambda: observed(record).get(record["instance_id"]) == "healthy",
            "resized ALB target health",
            timeout=120,
            interval=5,
        )
    record["frontend_restored_at"] = utc_now()
    save(record)
