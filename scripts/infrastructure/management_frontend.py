"""Two-phase management frontend activation with fresh, verified HTTPS evidence."""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import socket
import ssl
import urllib.request
from urllib.parse import urlparse

from common import CONFIG, Aws, private_dir, state_home, tf, utc_now, write_private
from management_acceptance import https_probe
from management_release import ORIGIN


def folder():
    return private_dir(state_home() / "cluster/frontend")


def publish_requested() -> bool:
    path = folder() / "activation.json"
    if not path.exists():
        return False
    value = json.loads(path.read_text())
    if type(value.get("published")) is not bool:
        raise ValueError("Frontend activation intent is malformed")
    return value["published"]


def manifest() -> dict:
    value = json.loads(tf("frontend", "output", "-json", "frontend_manifest"))
    expected = {k: CONFIG[k] for k in ("project", "account_id", "region", "cluster_id")}
    if (
        any(value.get(k) != v for k, v in expected.items())
        or value.get("origin") != ORIGIN
        or value.get("hostname") != urlparse(ORIGIN).hostname
        or value.get("node_port") != 30443
        or len(value.get("nodes", {})) != 3
        or not value.get("dns_name", "").endswith(".eu-west-2.elb.amazonaws.com")
    ):
        raise ValueError("Frontend manifest does not identify this project's allocation")
    return value


def binding(value):
    return {
        k: value[k]
        for k in (
            "project",
            "account_id",
            "region",
            "cluster_id",
            "hostname",
            "load_balancer_arn",
            "target_group_arn",
            "nodes",
        )
    }


class FrontendConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, connect_host, **kwargs):
        if host != urlparse(ORIGIN).hostname:
            raise ValueError("Frontend proof cannot redirect to another host")
        super().__init__(host, **kwargs)
        self.connect_host = connect_host

    def connect(self):
        if self._context.verify_mode != ssl.CERT_REQUIRED or not self._context.check_hostname:
            raise ValueError("Frontend proof requires public certificate and hostname verification")
        raw = socket.create_connection((self.connect_host, 443), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class FrontendHandler(urllib.request.HTTPSHandler):
    def __init__(self, host):
        super().__init__(context=ssl.create_default_context())
        self.host = host

    def https_open(self, request):
        return self.do_open(
            lambda host, **kw: FrontendConnection(host, connect_host=self.host, **kw),
            request,
            context=self._context,
        )


def check_targets(value: dict, aws: Aws) -> dict:
    """An imported declaration cannot substitute for live ALB/target identities."""
    load_balancer = aws.call(
        "elbv2", "describe-load-balancers", "--load-balancer-arns", value["load_balancer_arn"]
    )["LoadBalancers"]
    if (
        len(load_balancer) != 1
        or load_balancer[0]["State"]["Code"] != "active"
        or load_balancer[0]["DNSName"] != value["dns_name"]
        or load_balancer[0]["Scheme"] != "internet-facing"
    ):
        raise ValueError("Management load balancer is not the active expected frontend")
    tags = aws.call("elbv2", "describe-tags", "--resource-arns", value["load_balancer_arn"])
    observed = {t["Key"]: t["Value"] for t in tags["TagDescriptions"][0]["Tags"]}
    if any(
        observed.get(k) != v
        for k, v in {
            "Project": CONFIG["project"],
            "Cluster": CONFIG["cluster_id"],
            "ManagedBy": "terraform-platform-frontend",
        }.items()
    ):
        raise ValueError("Frontend live ownership tags differ")
    targets = aws.call(
        "elbv2", "describe-target-health", "--target-group-arn", value["target_group_arn"]
    )["TargetHealthDescriptions"]
    expected = {(instance, value["node_port"]) for instance in value["nodes"].values()}
    if (
        len(targets) != 3
        or {(t["Target"]["Id"], t["Target"]["Port"]) for t in targets} != expected
        or any(t["TargetHealth"]["State"] != "healthy" for t in targets)
    ):
        raise ValueError("All three exact management frontend targets must be healthy")
    return {
        "targets": [
            {"instance_id": t["Target"]["Id"], "state": t["TargetHealth"]["State"]} for t in targets
        ]
    }


def verify(value: dict | None = None) -> dict:
    value = value or manifest()
    aws = Aws("kubernetes-llm-bootstrap")
    aws.preflight("frontend")
    result = check_targets(value, aws)
    result["https"] = https_probe(None, handler=FrontendHandler(value["dns_name"]))
    result.update(binding(value))
    result["verified_at"] = utc_now()
    write_private(folder() / "verification.json", result)
    return result


def validate_activation() -> dict:
    path = folder() / "activation.json"
    if not path.exists():
        raise ValueError("Frontend publication needs explicit verified activation intent")
    intent = json.loads(path.read_text())
    value = manifest()
    if intent.get("published") is not True or any(
        intent.get(k) != v for k, v in binding(value).items()
    ):
        raise ValueError("Frontend activation belongs to a different allocation")
    return verify(value)


def activate(*, published: bool) -> dict:
    with (folder() / "activation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = manifest()
        if published:
            verify(value)
        intent = {**binding(value), "published": published, "recorded_at": utc_now()}
        write_private(folder() / "activation.json", intent)
        return intent


def cost() -> dict:
    aws = Aws("kubernetes-llm-bootstrap", region="us-east-1")
    rates = {}
    for sku in ("6AP766DZF74JPTE2", "D4JRRS3AFPHRV7RF"):
        data = aws.call(
            "pricing",
            "get-products",
            "--service-code",
            "AWSELB",
            "--filters",
            json.dumps([{"Type": "TERM_MATCH", "Field": "sku", "Value": sku}]),
        )
        products = [json.loads(p) for p in data["PriceList"]]
        if len(products) != 1 or products[0]["product"]["attributes"]["location"] != "EU (London)":
            raise ValueError("Frontend price identity changed; review the estimate")
        prices = [
            float(d["pricePerUnit"]["USD"])
            for term in products[0]["terms"]["OnDemand"].values()
            for d in term["priceDimensions"].values()
        ]
        if len(prices) != 1:
            raise ValueError("Frontend pricing needs manual review")
        rates[sku] = prices[0]
    hours = 730
    result = {
        "estimated_at": utc_now(),
        "account_id": CONFIG["account_id"],
        "region": CONFIG["region"],
        "scope": (
            "one three-zone HTTPS ALB, three public IPv4 addresses, "
            "one WAF ACL with two rate rules; no added VMs or GPUs"
        ),
        "hours": hours,
        "illustrative_average_lcus": 1,
        "aws_price_list_rates_usd": rates,
        "monthly_usd": round(
            hours * (rates["6AP766DZF74JPTE2"] + rates["D4JRRS3AFPHRV7RF"] + 3 * 0.005) + 5 + 2, 2
        ),
        "excluded": [
            "WAF requests at $0.60/million",
            "traffic-dependent additional LCU/IP usage",
            "data transfer",
            "DNS query charges",
            "tax",
        ],
        "sources": [
            "AWS Price List API AWSELB London",
            "https://aws.amazon.com/vpc/pricing/",
            "https://aws.amazon.com/waf/pricing/",
        ],
    }
    write_private(folder() / "cost.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify", "activate", "deactivate", "cost"])
    args = parser.parse_args()
    result = (
        activate(published=args.command == "activate")
        if args.command in {"activate", "deactivate"}
        else (cost() if args.command == "cost" else verify())
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
