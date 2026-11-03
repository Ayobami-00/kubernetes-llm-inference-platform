"""Offline validation shared by operator preflight and infrastructure tests."""

from __future__ import annotations

import ipaddress
import re

from platform_contracts.inventory import NetworkInfo


def validate_configuration(config: dict, reserved_cidrs: list[str] | None = None) -> None:
    if not re.fullmatch(r"[0-9]{12}", config["account_id"]):
        raise ValueError("Invalid account ID")
    if config["region"] != "eu-west-2":
        raise ValueError("The reviewed foundation is in eu-west-2")
    if set(config["nodes"]) != {"cp-01", "cp-02", "cp-03"}:
        raise ValueError("Exactly the three stable control-plane slots are required")
    ranges = [
        ipaddress.IPv4Network(config[key], strict=True)
        for key in ("vpc_cidr", "wireguard_cidr", "pod_cidr", "service_cidr")
    ]
    ranges += [ipaddress.IPv4Network(value, strict=True) for value in reserved_cidrs or []]
    for i, network in enumerate(ranges):
        if any(network.overlaps(other) for other in ranges[i + 1 :]):
            raise ValueError("VPC, overlay and reserved CIDRs overlap")
    NetworkInfo.model_validate(
        {key: config[key] for key in ("wireguard_cidr", "pod_cidr", "service_cidr")}
    )
    zones, subnets, wireguard = set(), [], set()
    for index, name in enumerate(("cp-01", "cp-02", "cp-03")):
        node = config["nodes"][name]
        zone = node["availability_zone"]
        if zone != f"{config['region']}{'abc'[index]}" or zone in zones:
            raise ValueError(
                "Use three distinct AZs with the agreed cp-01/a, cp-02/b, cp-03/c mapping"
            )
        zones.add(zone)
        for subnet_key, address_key in (
            ("public_subnet_cidr", "private_ip"),
            ("api_subnet_cidr", "api_ip"),
        ):
            subnet = ipaddress.IPv4Network(node[subnet_key], strict=True)
            address = ipaddress.IPv4Address(node[address_key])
            if not subnet.subnet_of(ranges[0]) or any(subnet.overlaps(other) for other in subnets):
                raise ValueError("CPU/API subnets overlap or are outside the VPC")
            if (
                address not in subnet
                or int(address) - int(subnet.network_address) < 4
                or address == subnet.broadcast_address
            ):
                raise ValueError("Host address is outside its subnet or reserved by AWS")
            subnets.append(subnet)
        address = ipaddress.IPv4Address(node["wireguard_ip"])
        if (
            address not in ranges[1]
            or address in {ranges[1].network_address, ranges[1].broadcast_address}
            or address in wireguard
        ):
            raise ValueError("Invalid or duplicate WireGuard host address")
        wireguard.add(address)
    first, last = [ipaddress.IPv4Address(value) for value in config["worker_wireguard_range"]]
    if (
        first not in ranges[1]
        or last not in ranges[1]
        or first > last
        or first == ranges[1].network_address
        or last == ranges[1].broadcast_address
        or any(first <= host <= last for host in wireguard)
    ):
        raise ValueError("Worker range conflicts with foundation hosts or the WireGuard network")


def validate_origins(origins: dict) -> None:
    if not isinstance(origins, dict):
        raise ValueError("Origins must be a map keyed by stable origin ID")
    seen = set()
    for name, origin in origins.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            raise ValueError("Origin IDs must be stable kebab-case names")
        if (
            not isinstance(origin, dict)
            or set(origin) - {"ipv4", "weight"}
            or not isinstance(origin.get("ipv4"), str)
        ):
            raise ValueError("Each origin requires an IPv4 string and optional weight only")
        address = ipaddress.IPv4Address(origin["ipv4"])
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError("Gateway origins require globally routable public IPv4 addresses")
        if address in seen:
            raise ValueError("Duplicate gateway origin address")
        weight = origin.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 255:
            raise ValueError("Origin weights must be integers from 1 to 255")
        seen.add(address)
