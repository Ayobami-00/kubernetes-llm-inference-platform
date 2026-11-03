"""The federation probe uses explicit projected credentials, not node or static keys."""

import copy
from types import SimpleNamespace

import pytest
from common import CONFIG
from federation_probe import job_document, verify_probe_policy


def document(**overrides):
    fields = {
        "name": "identity-test",
        "namespace": "platform-identity-tests",
        "service_account": "identity-probe",
        "audience": "sts.amazonaws.com",
        "allow": True,
        "role": f"arn:aws:iam::{CONFIG['account_id']}:role/{CONFIG['project']}/klip-identity-probe",
        "bucket": f"klip-identity-{CONFIG['account_id']}-{CONFIG['region']}",
        "node": "platform-01",
    }
    return job_document(**{**fields, **overrides})


def test_only_projected_sts_token_is_mounted_and_metadata_fallback_is_disabled():
    job = document()
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert pod["automountServiceAccountToken"] is False
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["AWS_MAX_ATTEMPTS"] == "1"
    assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/var/run/klip-identity/token"
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}.intersection(env)
    projection = next(v for v in pod["volumes"] if v["name"] == "token")["projected"]["sources"]
    assert projection == [
        {
            "serviceAccountToken": {
                "audience": "sts.amazonaws.com",
                "expirationSeconds": 600,
                "path": "token",
            }
        }
    ]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert job["spec"]["backoffLimit"] == 0 and pod["restartPolicy"] == "Never"
    assert "@sha256:" in container["image"]
    assert pod["nodeSelector"]["kubernetes.io/hostname"] == "platform-01"


@pytest.mark.parametrize(
    "overrides",
    [{"role": "arn:aws:iam::127811062476:role/admin"}, {"bucket": "application-private-data"}],
)
def test_probe_cannot_be_redirected_to_privileged_roles_or_application_data(overrides):
    with pytest.raises(ValueError):
        document(**overrides)


def test_negative_case_keeps_the_actual_requested_identity_and_audience():
    pod = document(
        namespace="other-namespace",
        service_account="wrong-service-account",
        audience="wrong-audience",
        allow=False,
    )["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "wrong-service-account"
    token = next(v for v in pod["volumes"] if v["name"] == "token")["projected"]["sources"][0]
    assert token["serviceAccountToken"]["audience"] == "wrong-audience"
    assert "KLIP_AUTH_ERROR_UNCLASSIFIED" in pod["containers"][0]["command"][2]


@pytest.mark.parametrize("drift", [None, "subject", "audience", "actions", "attached-policy"])
def test_live_iam_drift_is_rejected_before_projecting_any_credentials(drift):
    allocation = {
        "issuer": "https://oidc.klip-lab.click",
        "provider_arn": "provider",
        "bucket": f"klip-identity-{CONFIG['account_id']}-{CONFIG['region']}",
    }
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Principal": {"Federated": "provider"},
                "Condition": {
                    "StringEquals": {
                        "oidc.klip-lab.click:aud": "sts.amazonaws.com",
                        "oidc.klip-lab.click:sub": (
                            "system:serviceaccount:platform-identity-tests:identity-probe"
                        ),
                    }
                },
            }
        ],
    }
    permissions = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{allocation['bucket']}/{key}"
                    for key in (
                        ".well-known/openid-configuration",
                        "openid/v1/jwks",
                    )
                ],
            }
        ],
    }
    mutated = copy.deepcopy(trust)
    if drift == "subject":
        mutated["Statement"][0]["Condition"]["StringEquals"]["oidc.klip-lab.click:sub"] = "*"
    if drift == "audience":
        mutated["Statement"][0]["Condition"]["StringEquals"]["oidc.klip-lab.click:aud"] = "other"
    if drift == "actions":
        permissions["Statement"][0]["Action"] = ["s3:*"]

    def call(service, action, *args):
        return {
            "get-role": {"Role": {"AssumeRolePolicyDocument": mutated}},
            "get-role-policy": {"PolicyDocument": permissions},
            "list-role-policies": {"PolicyNames": ["public-signing-metadata-only"]},
            "list-attached-role-policies": {
                "AttachedPolicies": [{"PolicyArn": "admin"}] if drift == "attached-policy" else []
            },
        }[action]

    aws = SimpleNamespace(call=call)
    if drift is None:
        verify_probe_policy(aws, allocation)
    else:
        with pytest.raises(ValueError):
            verify_probe_policy(aws, allocation)
