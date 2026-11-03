"""Retained telemetry allocation, private email input and bounded delivery evidence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
from datetime import UTC, datetime

from common import CONFIG, Aws, private_dir, state_home, tf, utc_now, write_private


def folder():
    return private_dir(state_home() / "cluster/observability")


def receiver() -> dict:
    value = json.loads((folder() / "alert-receiver.json").read_text())
    endpoint = value.get("endpoint", "")
    if (
        value.get("protocol") != "email"
        or not isinstance(endpoint, str)
        or len(endpoint) > 254
        or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", endpoint
        )
    ):
        raise ValueError("A private, user-authorized email receiver is required")
    try:
        authorization = value["authorized_at"]
        if not isinstance(authorization, str):
            raise ValueError("Invalid receiver authorization time")
        authorized = datetime.fromisoformat(authorization.replace("Z", "+00:00"))
        if authorized.tzinfo is None or authorized > datetime.now(UTC):
            raise ValueError("Invalid receiver authorization time")
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Record the receiver authorization time explicitly") from exc
    return value


def manifest() -> dict:
    value = json.loads(tf("observability", "output", "-json", "observability_manifest"))
    expected = {k: CONFIG[k] for k in ("project", "account_id", "region", "cluster_id")}
    if (
        any(value.get(k) != v for k, v in expected.items())
        or value.get("schema_version") != "klip-observability-allocation.v1"
        or value.get("namespace") != "observability"
        or set(value.get("buckets", {})) != {"metrics", "logs", "traces"}
        or value.get("alerts", {}).get("region") != "us-east-1"
        or value["alerts"].get("topic_arn")
        != f"arn:aws:sns:us-east-1:{CONFIG['account_id']}:klip-platform-alerts"
        or value["alerts"].get("hostname") != f"management.{CONFIG['domain']}"
        or value["alerts"].get("path") != "/health/ready"
    ):
        raise ValueError("Unexpected observability allocation identity")
    return value


def cost() -> dict:
    """Conservative gross estimate; no free-tier or AWS-endpoint discounts assumed."""
    return {
        "priced_at": "2026-11-03",
        "currency": "USD",
        "monthly_fixed_estimate": 2.60,
        "fixed_items": {
            "route53_https_health_check": 1.50,
            "standard_alarm": 0.10,
            "kms_key": 1.00,
        },
        "variable_rates": {
            "s3_standard_eu_west_2_per_gb_month_first_50tb": 0.024,
            "sns_email_per_100000": 2.00,
            "sns_publish_per_million": 0.50,
            "kms_per_10000_symmetric_requests": 0.03,
        },
        "example": {"current_and_noncurrent_s3_gb": 100, "s3_storage_monthly": 2.40},
        "exclusions": [
            "S3 PUT/GET/LIST, data transfer, noncurrent versions above the example, taxes",
            "Kubernetes releases, EBS telemetry volumes and any future worker resizing",
            "KMS first and second key rotations each add $1/month when they occur",
        ],
        "resources": (
            "3 retained S3 buckets, 6 exact-subject roles, 1 KMS key, "
            "1 SNS topic/email subscription, 1 health check/alarm; no compute or GPU allocation"
        ),
        "sources": [
            "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/eu-west-2/index.json",
            "https://aws.amazon.com/route53/pricing/",
            "https://aws.amazon.com/cloudwatch/pricing/",
            "https://aws.amazon.com/sns/pricing/",
            "https://aws.amazon.com/kms/pricing/",
        ],
    }


def verify() -> dict:
    allocation = manifest()
    aws = Aws("kubernetes-llm-bootstrap")
    aws.preflight("observability")
    buckets = {}
    for kind, item in allocation["buckets"].items():
        name = item["name"]
        if name != f"klip-{kind}-{CONFIG['account_id']}-{CONFIG['region']}":
            raise ValueError("Unexpected telemetry bucket")
        args = ["--bucket", name, "--expected-bucket-owner", CONFIG["account_id"]]
        versioning = aws.call("s3api", "get-bucket-versioning", *args)
        encryption = aws.call("s3api", "get-bucket-encryption", *args)
        public = aws.call("s3api", "get-public-access-block", *args)[
            "PublicAccessBlockConfiguration"
        ]
        if (
            versioning.get("Status") != "Enabled"
            or not all(
                public.get(k) is True
                for k in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            )
            or encryption["ServerSideEncryptionConfiguration"]["Rules"][0][
                "ApplyServerSideEncryptionByDefault"
            ]["SSEAlgorithm"]
            != "AES256"
        ):
            raise ValueError("Telemetry storage encryption, privacy or versioning differs")
        buckets[kind] = {
            "name": name,
            "encrypted": True,
            "versioned": True,
            "public_access_blocked": True,
        }
    alerts = allocation["alerts"]
    global_aws = Aws("kubernetes-llm-bootstrap", alerts["region"])
    topic = global_aws.call("sns", "get-topic-attributes", "--topic-arn", alerts["topic_arn"])[
        "Attributes"
    ]
    if topic.get("KmsMasterKeyId") != alerts["key_arn"]:
        raise ValueError("Alert topic encryption differs")
    subscriptions = global_aws.call(
        "sns", "list-subscriptions-by-topic", "--topic-arn", alerts["topic_arn"]
    )["Subscriptions"]
    destination = receiver()["endpoint"]
    matched = [
        s
        for s in subscriptions
        if s.get("Protocol") == "email" and s.get("Endpoint") == destination
    ]
    if len(matched) != 1 or len(subscriptions) != 1:
        raise ValueError("Alert destination missing, duplicated or contains an unreviewed receiver")
    subscription_arn = matched[0]["SubscriptionArn"]
    confirmed = False
    if subscription_arn != "PendingConfirmation":
        if not subscription_arn.startswith(alerts["topic_arn"] + ":"):
            raise ValueError("Unexpected email subscription identity")
        subscription = global_aws.call(
            "sns", "get-subscription-attributes", "--subscription-arn", subscription_arn
        )["Attributes"]
        confirmed = subscription.get("PendingConfirmation") == "false"
    alarm = global_aws.call("cloudwatch", "describe-alarms", "--alarm-names", alerts["alarm_name"])[
        "MetricAlarms"
    ]
    if (
        len(alarm) != 1
        or alarm[0].get("AlarmArn") != alerts["alarm_arn"]
        or alarm[0].get("Namespace") != "AWS/Route53"
        or alarm[0].get("MetricName") != "HealthCheckStatus"
        or alarm[0].get("Dimensions")
        != [{"Name": "HealthCheckId", "Value": alerts["health_check_id"]}]
        or alarm[0].get("AlarmActions") != [alerts["topic_arn"]]
        or alarm[0].get("OKActions") != [alerts["topic_arn"]]
        or alarm[0].get("ActionsEnabled") is not True
        or alarm[0].get("TreatMissingData") != "breaching"
        or alarm[0].get("Statistic") != "Minimum"
        or alarm[0].get("Period") != 60
        or alarm[0].get("EvaluationPeriods") != 2
        or alarm[0].get("DatapointsToAlarm") != 2
        or alarm[0].get("Threshold") != 1
        or alarm[0].get("ComparisonOperator") != "LessThanThreshold"
    ):
        raise ValueError("External alarm differs from the reviewed Route 53 path")
    health = global_aws.call(
        "route53", "get-health-check", "--health-check-id", alerts["health_check_id"]
    )["HealthCheck"]["HealthCheckConfig"]
    if (
        health.get("FullyQualifiedDomainName") != alerts["hostname"]
        or health.get("ResourcePath") != alerts["path"]
        or health.get("Type") != "HTTPS"
        or health.get("EnableSNI") is not True
        or health.get("Disabled", False)
        or health.get("Inverted", False)
        or health.get("RequestInterval") != 30
        or health.get("FailureThreshold") != 3
        or set(health.get("Regions", [])) != {"eu-west-1", "us-east-1", "ap-southeast-1"}
    ):
        raise ValueError("External health check no longer targets management readiness")
    result = {
        "verified_at": utc_now(),
        "allocation": allocation,
        "buckets": buckets,
        "email_subscription_confirmed": confirmed,
        "external_alarm_state": alarm[0]["StateValue"],
        "limitations": [
            "Allocation verification does not prove deployed telemetry, email receipt "
            "or outage detection.",
            "Route 53 HTTPS checks do not validate certificates; blackbox will cover "
            "TLS separately once deployed.",
        ],
    }
    write_private(folder() / "allocation-verification.json", result)
    return result


def send_test(operation: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{12}", operation):
        raise ValueError("Use a distinct 12-character hexadecimal notification operation")
    with (folder() / "notifications.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder() / f"notification-{operation}.json"
        if path.exists():
            # Never resend on ambiguous failures, retries or after terminal success.
            return json.loads(path.read_text())
        proof = verify()
        if not proof["email_subscription_confirmed"]:
            raise ValueError("Email subscription awaits the recipient's confirmation click")
        message = (
            f"KLIP authorized operational delivery test {operation}.\n"
            "This tests SNS email delivery, not an actual outage.\n"
            "Management readiness: https://management.klip-lab.click/health/ready\n"
            "Please confirm receipt in the project chat; do not share subscription URLs or codes."
        )
        record = {
            "operation": operation,
            "created_at": utc_now(),
            "state": "submission_pending",
            "topic_arn": proof["allocation"]["alerts"]["topic_arn"],
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "recipient_receipt_verified": False,
        }
        write_private(path, record)
        response = Aws("kubernetes-llm-bootstrap", "us-east-1").call(
            "sns",
            "publish",
            "--topic-arn",
            record["topic_arn"],
            "--subject",
            "KLIP operational alert delivery test",
            "--message",
            message,
        )
        record.update(state="sns_accepted", accepted_at=utc_now(), message_id=response["MessageId"])
        write_private(path, record)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["cost", "verify", "test-notification"])
    parser.add_argument("--operation")
    args = parser.parse_args()
    if args.command == "cost":
        value = cost()
        write_private(folder() / "cost.json", value)
    elif args.command == "verify":
        value = verify()
    else:
        value = send_test(args.operation or "")
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
