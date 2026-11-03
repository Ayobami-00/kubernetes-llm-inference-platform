"""Unexpected permissions or federation subjects prevent telemetry deployment."""

import copy
import json

import observability
import pytest


def test_actual_roles_must_match_all_six_applied_documents(monkeypatch, tmp_path):
    subjects, resources, expected = {}, [], {}
    for i in range(6):
        name = f"synthetic-role-{i}"
        arn = "arn:aws:iam::123456789012:role/test/" + name
        trust = {"synthetic-subject": "system:serviceaccount:observability:" + name}
        policy = {"synthetic-action": "only-test-resource"}
        subjects[name] = {"role_arn": arn, "name": name}
        expected[name] = {
            "Arn": arn,
            "MaxSessionDuration": 3600,
            "AssumeRolePolicyDocument": trust,
        }
        resources.extend(
            [
                {
                    "type": "aws_iam_role",
                    "values": {"arn": arn, "name": name, "assume_role_policy": json.dumps(trust)},
                },
                {
                    "type": "aws_iam_role_policy",
                    "values": {"role": name, "name": "only", "policy": json.dumps(policy)},
                },
            ]
        )
    monkeypatch.setattr(observability, "manifest", lambda: {"service_accounts": subjects})
    monkeypatch.setattr(observability, "folder", lambda: tmp_path)
    monkeypatch.setattr(
        observability,
        "tf",
        lambda *_: json.dumps({"values": {"root_module": {"resources": resources}}}),
    )

    class Aws:
        extra = []

        def __init__(self, *_args):
            pass

        def preflight(self, *_args):
            pass

        def call(self, _service, action, *args):
            if action == "get-role":
                return {"Role": expected[args[1]]}
            if action == "list-attached-role-policies":
                return {"AttachedPolicies": self.extra}
            if action == "list-role-policies":
                return {"PolicyNames": ["only"]}
            if action == "get-role-policy":
                return {"PolicyDocument": {"synthetic-action": "only-test-resource"}}
            raise AssertionError("Unexpected IAM action")

    monkeypatch.setattr(observability, "Aws", Aws)
    assert len(observability.verify_roles()["roles"]) == 6
    before = (tmp_path / "role-verification.json").read_bytes()
    original = copy.deepcopy(expected)
    expected["synthetic-role-2"]["AssumeRolePolicyDocument"] = {"synthetic-subject": "*"}
    with pytest.raises(ValueError, match="drifted"):
        observability.verify_roles()
    assert (tmp_path / "role-verification.json").read_bytes() == before
    expected.update(original)
    Aws.extra = [{"PolicyArn": "synthetic-unexpected-admin-policy"}]
    with pytest.raises(ValueError, match="drifted"):
        observability.verify_roles()
