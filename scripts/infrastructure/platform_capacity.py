"""Read-only regional headroom checks immediately before a paid worker apply."""

from __future__ import annotations

import math
import re
from collections import Counter


def standard_instance(instance_type: str) -> bool:
    # AWS's Standard quota covers A/C/D/H/I/M/R/T/Z, including Im/Is storage
    # variants; accelerators and HPC have separate quota families.
    return re.match(r"^(?:[acdhimrtz]|im|is)[0-9]", instance_type) is not None


def quota(aws, code: str) -> float:
    value = aws.call(
        "service-quotas", "get-service-quota", "--service-code", "ec2", "--quota-code", code
    )["Quota"]["Value"]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("A verified regional EC2 quota is required before provisioning")
    return value


def ensure_capacity(aws, document: dict) -> dict:
    types = Counter()
    addresses = 0
    for resource in document.get("resource_changes", []):
        if resource.get("mode") == "data":
            continue
        change = resource["change"]
        if resource["type"] == "aws_eip" and "create" in change["actions"]:
            addresses += 1
        if resource["type"] != "aws_instance":
            continue
        after, before = change.get("after") or {}, change.get("before") or {}
        if "create" in change["actions"]:
            types[after["instance_type"]] += 1
        elif change["actions"] == ["update"] and after["instance_type"] != before["instance_type"]:
            types[after["instance_type"]] += 1
            types[before["instance_type"]] -= 1
    evidence = {}
    if addresses:
        used = len(aws.call("ec2", "describe-addresses")["Addresses"])
        limit = quota(aws, "L-0263D0A3")
        evidence["elastic_ips"] = {"used": used, "additional": addresses, "quota": limit}
        if used + addresses > limit:
            raise ValueError(
                f"Platform requires {used + addresses} Elastic IPs; regional quota is {limit:g}"
            )
    if types:
        if not all(standard_instance(name) for name in types):
            raise ValueError("Persistent platform workers must use Standard on-demand CPU types")
        reservations = aws.call(
            "ec2",
            "describe-instances",
            "--filters",
            "Name=instance-state-name,Values=pending,running,stopping",
        )["Reservations"]
        active = [
            item["InstanceType"]
            for reservation in reservations
            for item in reservation["Instances"]
            if item.get("InstanceLifecycle") != "spot" and standard_instance(item["InstanceType"])
        ]
        names = sorted(set(active) | set(types))
        observed = aws.call("ec2", "describe-instance-types", "--instance-types", *names)[
            "InstanceTypes"
        ]
        cpus = {item["InstanceType"]: item["VCpuInfo"]["DefaultVCpus"] for item in observed}
        if set(cpus) != set(names) or any(
            type(value) is not int or value < 1 for value in cpus.values()
        ):
            raise ValueError("Incomplete instance-type capacity evidence")
        used = sum(cpus[name] for name in active)
        additional = max(0, sum(cpus[name] * count for name, count in types.items()))
        limit = quota(aws, "L-1216C47A")
        evidence["standard_vcpus"] = {"used": used, "additional": additional, "quota": limit}
        if used + additional > limit:
            raise ValueError(
                f"Platform requires {used + additional} Standard vCPUs; regional quota is {limit:g}"
            )
    return evidence
