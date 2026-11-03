"""Private destination handling and no-retry notification evidence."""

import json
from unittest.mock import Mock

import observability
import pytest


def test_receiver_requires_explicit_safe_authorized_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "folder", lambda: tmp_path)
    path = tmp_path / "alert-receiver.json"
    valid = {
        "protocol": "email",
        "endpoint": "operator@example.invalid",
        "authorized_at": "2026-01-01T00:00:00Z",
    }
    path.write_text(json.dumps(valid))
    assert observability.receiver() == valid
    for override in (
        {"protocol": "sms"},
        {"endpoint": "user@example.invalid\nsecret"},
        {"endpoint": "x" * 300 + "@example.invalid"},
        {"authorized_at": "2099-01-01T00:00:00Z"},
        {"authorized_at": "2026-01-01T00:00:00"},
        {"authorized_at": None},
    ):
        path.write_text(json.dumps({**valid, **override}))
        with pytest.raises(ValueError):
            observability.receiver()


def setup_notification(tmp_path, monkeypatch, confirmed=True):
    monkeypatch.setattr(observability, "folder", lambda: tmp_path)
    verify = Mock(
        return_value={
            "email_subscription_confirmed": confirmed,
            "allocation": {"alerts": {"topic_arn": "topic"}},
        }
    )
    monkeypatch.setattr(observability, "verify", verify)
    aws = Mock()
    aws.call.return_value = {"MessageId": "accepted-id"}
    monkeypatch.setattr(observability, "Aws", Mock(return_value=aws))
    return verify, aws


def test_pending_email_is_never_published(tmp_path, monkeypatch):
    _, aws = setup_notification(tmp_path, monkeypatch, confirmed=False)
    with pytest.raises(ValueError, match="confirmation"):
        observability.send_test("123456abcdef")
    aws.call.assert_not_called()
    assert not (tmp_path / "notification-123456abcdef.json").exists()


def test_accepted_notification_reuses_identity_without_another_publish(tmp_path, monkeypatch):
    verify, aws = setup_notification(tmp_path, monkeypatch)
    value = observability.send_test("123456abcdef")
    assert value["state"] == "sns_accepted" and not value["recipient_receipt_verified"]
    verify.side_effect = RuntimeError("Credentials expired after submission")
    assert observability.send_test("123456abcdef") == value
    assert aws.call.call_count == 1


def test_ambiguous_notification_is_not_automatically_retried(tmp_path, monkeypatch):
    verify, aws = setup_notification(tmp_path, monkeypatch)
    aws.call.side_effect = RuntimeError("Response lost after remote acceptance")
    with pytest.raises(RuntimeError):
        observability.send_test("123456abcdef")
    verify.side_effect = RuntimeError("Do not need credentials to observe prior intent")
    value = observability.send_test("123456abcdef")
    assert value["state"] == "submission_pending" and not value["recipient_receipt_verified"]
    assert aws.call.call_count == 1


@pytest.mark.parametrize("operation", ["", "../123456abc", "123456abcdeG"])
def test_notification_identity_is_path_safe(operation):
    with pytest.raises(ValueError):
        observability.send_test(operation)


def test_receipt_acknowledgment_requires_submission_and_never_republishes(tmp_path, monkeypatch):
    _, aws = setup_notification(tmp_path, monkeypatch)
    observability.send_test("123456abcdef")
    confirmed = observability.acknowledge_receipt("123456abcdef")
    assert confirmed["recipient_receipt_verified"] and confirmed["receipt_confirmed_at"]
    assert observability.acknowledge_receipt("123456abcdef") == confirmed
    assert aws.call.call_count == 1
    path = tmp_path / "notification-123456abcdef.json"
    path.write_text(json.dumps({**confirmed, "state": "submission_pending"}))
    with pytest.raises(ValueError, match="submitted"):
        observability.acknowledge_receipt("123456abcdef")
