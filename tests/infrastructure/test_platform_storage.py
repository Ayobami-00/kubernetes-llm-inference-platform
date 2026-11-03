"""Policy drift must never be normalized into a broader storage grant."""

import copy

import pytest
from platform_storage import assert_policy


def test_policy_order_is_ignored_but_privilege_changes_fail():
    expected = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject"],
                "Resource": "arn:aws:s3:::fixture/postgresql/*",
                "Condition": {"StringEquals": {"fixture:sub": ["db", "restore"]}},
            },
            {
                "Effect": "Deny",
                "Action": "s3:*",
                "Resource": "*",
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }
    reordered = copy.deepcopy(expected)
    reordered["Statement"][0]["Action"].reverse()
    reordered["Statement"].reverse()
    assert_policy(reordered, expected, "fixture")
    single = {"Statement": [{"Action": ["s3:GetObject"], "Resource": ["fixture"]}]}
    assert_policy(
        single, {"Statement": {"Action": "s3:GetObject", "Resource": "fixture"}}, "singleton"
    )
    for change in ("resource", "action", "subject", "deny"):
        actual = copy.deepcopy(expected)
        if change == "resource":
            actual["Statement"][0]["Resource"] = "*"
        if change == "action":
            actual["Statement"][0]["Action"].append("s3:DeleteObjectVersion")
        if change == "subject":
            actual["Statement"][0]["Condition"]["StringEquals"]["fixture:sub"].append("foreign")
        if change == "deny":
            actual["Statement"].pop()
        with pytest.raises(ValueError, match="differs from applied policy"):
            assert_policy(actual, expected, "fixture")
