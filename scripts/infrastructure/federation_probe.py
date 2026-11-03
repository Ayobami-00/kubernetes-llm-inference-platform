"""Real projected-token/standard-SDK federation checks, without retaining credentials."""

from __future__ import annotations

import json
import re
import uuid

from common import CONFIG, PROFILES, ROOT, Aws, source_digest, tf, utc_now, write_private
from smoke import wait_until

NAMESPACE = "platform-identity-tests"
OTHER_NAMESPACE = "platform-identity-other-tests"
SERVICE_ACCOUNT = "identity-probe"

ALLOW = """
set -eu
aws sts get-caller-identity --output json >/tmp/caller.json 2>/tmp/aws-error || {
  echo KLIP_STS_FAILED; exit 1;
}
aws s3api get-object --bucket "$KLIP_PROBE_BUCKET" --key openid/v1/jwks /tmp/jwks.json \
  >/tmp/object-response 2>/tmp/aws-error || { echo KLIP_METADATA_READ_FAILED; exit 1; }
if aws s3api list-objects-v2 --bucket "$KLIP_PROBE_BUCKET" --max-items 1 \
  >/tmp/list-response 2>/tmp/aws-error; then echo KLIP_UNEXPECTED_LIST_ACCESS; exit 1; fi
grep -q '(AccessDenied)' /tmp/aws-error || { echo KLIP_LIST_ERROR_UNCLASSIFIED; exit 1; }
cat /tmp/caller.json
echo KLIP_METADATA_READ_OK
echo KLIP_BUCKET_LIST_DENIED
"""
DENY = """
set -eu
if aws sts get-caller-identity --output json >/tmp/caller.json 2>/tmp/aws-error; then
  echo KLIP_UNEXPECTED_IDENTITY; exit 1;
fi
if grep -q '(AccessDenied)' /tmp/aws-error; then echo KLIP_AUTH_DENIED:AccessDenied;
elif grep -q '(InvalidIdentityToken)' /tmp/aws-error; then
  echo KLIP_AUTH_DENIED:InvalidIdentityToken;
else echo KLIP_AUTH_ERROR_UNCLASSIFIED; exit 1; fi
"""


def verify_probe_policy(aws, allocation: dict) -> None:
    role = "klip-identity-probe"
    issuer = allocation["issuer"].removeprefix("https://")
    actual = aws.call("iam", "get-role", "--role-name", role)["Role"]["AssumeRolePolicyDocument"]
    expected = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Principal": {"Federated": allocation["provider_arn"]},
                "Condition": {
                    "StringEquals": {
                        issuer + ":aud": "sts.amazonaws.com",
                        issuer + ":sub": f"system:serviceaccount:{NAMESPACE}:{SERVICE_ACCOUNT}",
                    }
                },
            }
        ],
    }
    if actual != expected:
        raise ValueError(
            "Live canary role trust differs from the exact issuer/subject/audience binding"
        )
    attached = aws.call("iam", "list-attached-role-policies", "--role-name", role)[
        "AttachedPolicies"
    ]
    inline = aws.call("iam", "list-role-policies", "--role-name", role)["PolicyNames"]
    if attached or inline != ["public-signing-metadata-only"]:
        raise ValueError("Canary role has unexpected policies")
    policy = aws.call("iam", "get-role-policy", "--role-name", role, "--policy-name", inline[0])[
        "PolicyDocument"
    ]
    resources = [
        f"arn:aws:s3:::{allocation['bucket']}/{key}"
        for key in (".well-known/openid-configuration", "openid/v1/jwks")
    ]
    if policy != {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": resources,
            }
        ],
    }:
        raise ValueError("Live canary permissions exceed or differ from public metadata reads")


def probe_image() -> str:
    value = json.loads((ROOT / "deploy/platform-services.lock.json").read_text())[
        "verification_images"
    ]["aws_cli"]
    if value["platform"] != "linux/amd64" or not re.fullmatch(
        r"public\.ecr\.aws/aws-cli/aws-cli@sha256:[a-f0-9]{64}", value["image"]
    ):
        raise ValueError("Federation tests require the verified official AWS CLI digest")
    return value["image"]


def job_document(
    name: str,
    namespace: str,
    service_account: str,
    role: str,
    bucket: str,
    audience: str,
    allow: bool,
    node: str | None = None,
) -> dict:
    if role != f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-identity-probe":
        raise ValueError("Only the metadata-only canary role may be tested")
    if bucket != f"klip-identity-{CONFIG['account_id']}-{CONFIG['region']}":
        raise ValueError("Only the public-metadata origin bucket may be tested")
    selector = {"klip.dev/node-role": "platform"}
    if node:
        selector["kubernetes.io/hostname"] = node
    environment = {
        "AWS_ROLE_ARN": role,
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/klip-identity/token",
        "AWS_ROLE_SESSION_NAME": name,
        "AWS_DEFAULT_REGION": CONFIG["region"],
        "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_MAX_ATTEMPTS": "1",
        "AWS_PAGER": "",
        "HOME": "/tmp",
        "KLIP_PROBE_BUCKET": bucket,
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 150,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": "klip-identity-probe"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": service_account,
                    "automountServiceAccountToken": False,
                    "nodeSelector": selector,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": probe_image(),
                            "command": ["/bin/sh", "-ec", ALLOW if allow else DENY],
                            "env": [
                                {"name": key, "value": value} for key, value in environment.items()
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "volumeMounts": [
                                {
                                    "name": "token",
                                    "mountPath": "/var/run/klip-identity",
                                    "readOnly": True,
                                },
                                {"name": "scratch", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "scratch", "emptyDir": {"sizeLimit": "128Mi"}},
                        {
                            "name": "token",
                            "projected": {
                                "defaultMode": 0o444,
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "audience": audience,
                                            "expirationSeconds": 600,
                                            "path": "token",
                                        }
                                    }
                                ],
                            },
                        },
                    ],
                },
            },
        },
    }


def verify_federation() -> dict:
    from cluster import FOLDER, Host, kubectl, load_hosts

    aws = Aws(PROFILES["bootstrap"])
    aws.preflight("identity")
    allocation = json.loads(tf("identity", "output", "-json", "identity_manifest"))
    if (allocation["account_id"], allocation["cluster_id"], allocation["issuer"]) != (
        CONFIG["account_id"],
        CONFIG["cluster_id"],
        "https://oidc." + CONFIG["domain"],
    ):
        raise ValueError("Identity allocation differs from this cluster")
    transition = json.loads((FOLDER / "issuer-transition.json").read_text())
    if transition["status"] != "completed" or transition["issuer"] != allocation["issuer"]:
        raise ValueError("Complete the verified API issuer transition before federation tests")
    hosts = load_hosts()
    for name in CONFIG["nodes"]:
        actual = json.loads(
            Host(hosts[name]).run(
                [
                    "sudo",
                    "kubectl",
                    "--kubeconfig=/etc/kubernetes/admin.conf",
                    "--server=https://" + hosts[name]["klip_node"]["wireguard_ip"] + ":6443",
                    "--request-timeout=5s",
                    "get",
                    "--raw",
                    "/.well-known/openid-configuration",
                ],
                timeout=15,
            )
        )
        if actual["issuer"] != allocation["issuer"]:
            raise ValueError("A live API server no longer advertises the selected issuer")
    verify_probe_policy(aws, allocation)
    cp = Host(hosts["cp-01"])
    command = [
        "sudo",
        "kubectl",
        "--kubeconfig=/etc/kubernetes/admin.conf",
        "--request-timeout=10s",
    ]
    operation = uuid.uuid4().hex[:12]
    folder = FOLDER / "federation-probes" / operation
    report = {
        "schema_version": "klip-federation-probe.v1",
        "operation_id": operation,
        "status": "running",
        "started_at": utc_now(),
        "image": probe_image(),
        "source_sha256": source_digest(),
        "cases": {},
    }
    owned = {}

    def save():
        write_private(folder / "observation.json", report)
        write_private(FOLDER / "federation-probe.json", report)

    def create(document):
        return json.loads(
            cp.run(
                [*command, "create", "-f", "-", "-o", "json"], data=json.dumps(document), timeout=30
            )
        )

    save()
    try:
        for namespace in (NAMESPACE, OTHER_NAMESPACE):
            if kubectl("get", "namespace", namespace, "--ignore-not-found", "-o", "json").strip():
                raise ValueError(
                    "Federation test namespace already exists; inspect its prior owner"
                )
            value = create(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": namespace,
                        "annotations": {"klip.dev/verification-id": operation},
                        "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
                    },
                }
            )
            owned[namespace] = value["metadata"]["uid"]
            for account in (SERVICE_ACCOUNT, "wrong-service-account"):
                create(
                    {
                        "apiVersion": "v1",
                        "kind": "ServiceAccount",
                        "metadata": {"name": account, "namespace": namespace},
                        "automountServiceAccountToken": False,
                    }
                )
        nodes = json.loads((ROOT / "infra/terraform/platform.json").read_text())["nodes"]
        cases = [
            ("allow-" + name, NAMESPACE, SERVICE_ACCOUNT, "sts.amazonaws.com", True, name)
            for name in sorted(nodes)
        ] + [
            ("deny-account", NAMESPACE, "wrong-service-account", "sts.amazonaws.com", False, None),
            ("deny-namespace", OTHER_NAMESPACE, SERVICE_ACCOUNT, "sts.amazonaws.com", False, None),
            ("deny-audience", NAMESPACE, SERVICE_ACCOUNT, "untrusted-audience", False, None),
        ]
        for case, namespace, account, audience, allow, node in cases:
            name = case + "-" + operation
            document = job_document(
                name,
                namespace,
                account,
                allocation["probe_role_arn"],
                allocation["bucket"],
                audience,
                allow,
                node,
            )
            write_private(folder / (case + ".job.json"), document)
            created = create(document)
            report["cases"][case] = {"job_uid": created["metadata"]["uid"], "status": "running"}
            save()

            def terminal(namespace=namespace, name=name):
                status = json.loads(kubectl("-n", namespace, "get", "job", name, "-o", "json"))[
                    "status"
                ]
                return any(
                    item["type"] in {"Complete", "Failed"} and item["status"] == "True"
                    for item in status.get("conditions", [])
                )

            wait_until(terminal, "federation canary " + case, timeout=180, interval=3)
            result = json.loads(kubectl("-n", namespace, "get", "job", name, "-o", "json"))
            logs = kubectl("-n", namespace, "logs", "job/" + name)
            if re.search(
                r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\beyJ[A-Za-z0-9_-]+\.eyJ|SecretAccessKey|SessionToken",
                logs,
            ):
                raise ValueError(
                    "Credential-shaped content detected; canary logs were not retained"
                )
            write_private(folder / (case + ".log"), logs)
            if result["metadata"]["uid"] != created["metadata"]["uid"] or not result["status"].get(
                "succeeded"
            ):
                raise ValueError("Federation canary failed; inspect its sanitized private report")
            if allow:
                match = re.search(r'"Arn"\s*:\s*"([^"]+)"', logs)
                if (
                    not match
                    or not match[1].startswith(
                        f"arn:aws:sts::{CONFIG['account_id']}:assumed-role/klip-identity-probe/"
                    )
                    or "KLIP_METADATA_READ_OK" not in logs
                    or "KLIP_BUCKET_LIST_DENIED" not in logs
                ):
                    raise ValueError(
                        "Canary caller identity or scoped S3 checks differ from expectations"
                    )
                report["cases"][case].update(
                    status="passed",
                    caller_arn=match[1],
                    metadata_read=True,
                    bucket_list_denied=True,
                )
            else:
                if "KLIP_AUTH_DENIED:" not in logs:
                    raise ValueError("Expected an explicit AWS authentication denial")
                report["cases"][case].update(status="passed", authentication_denied=True)
            save()
        report.update(status="completed", completed_at=utc_now())
    except Exception as error:
        report.update(status="failed", error_code=type(error).__name__)
        raise
    finally:
        report["cleanup_pending"] = True
        try:
            for namespace, uid in owned.items():
                current = json.loads(kubectl("get", "namespace", namespace, "-o", "json"))
                if current["metadata"]["uid"] != uid:
                    raise ValueError("Federation namespace ownership changed; cleanup refused")
                kubectl("delete", "namespace", namespace, "--wait=false")
                wait_until(
                    lambda namespace=namespace: (
                        not kubectl(
                            "get", "namespace", namespace, "--ignore-not-found", "-o", "json"
                        ).strip()
                    ),
                    "canary cleanup",
                    timeout=60,
                )
            report["cleanup_pending"] = False
        except Exception:
            report["status"] = "failed"
            raise
        finally:
            save()
    return report
