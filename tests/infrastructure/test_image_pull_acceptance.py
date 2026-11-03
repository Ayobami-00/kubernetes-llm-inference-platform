"""Image checks must exercise registry authorization even after bytes are cached."""

import pytest
from image_pull_acceptance import denied, execution_complete, job_document

IMAGE = "127811062476.dkr.ecr.eu-west-2.amazonaws.com/klip/management-backend@sha256:" + "1" * 64


def test_positive_and_denied_checks_have_no_listener_secret_or_cached_pull_bypass():
    for case, namespace in [
        ("positive", "klip"),
        ("missing-annotation", "klip"),
        ("wrong-namespace", "klip-image-denied-012345abcdef"),
    ]:
        job = job_document("012345abcdef", case, "platform-01", IMAGE, namespace)
        pod = job["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert not pod["automountServiceAccountToken"]
        assert "volumes" not in pod and "env" not in container and "ports" not in container
        assert container["imagePullPolicy"] == "Always"
        assert container["command"] == [
            "/usr/local/bin/python",
            "-c",
            "print('private-image-pull-ok')",
        ]
        assert pod["securityContext"]["runAsUser"] == 10001
        assert job["spec"]["backoffLimit"] == 0


def test_network_and_transport_failures_do_not_count_as_identity_denials():
    assert denied({"reason": "ErrImagePull", "message": "failed to authorize: 403 Forbidden"})
    assert not denied({"reason": "ErrImagePull", "message": "connection timed out"})
    assert not denied({"reason": "ContainerCreating", "message": "401 Unauthorized"})


def test_verification_rejects_unpinned_foreign_images_and_control_plane_placement():
    with pytest.raises(ValueError, match="Unreviewed"):
        job_document("012345abcdef", "positive", "cp-01", IMAGE, "klip")
    with pytest.raises(ValueError, match="Unreviewed"):
        job_document(
            "012345abcdef", "positive", "platform-01", "public.example/test:latest", "klip"
        )


def test_pod_completion_before_job_status_is_not_an_execution_failure():
    assert not execution_complete("positive", {}, "Succeeded")
    assert execution_complete("positive", {"succeeded": 1}, "Succeeded")
    with pytest.raises(ValueError, match="unauthorized"):
        execution_complete("missing-annotation", {}, "Succeeded")
    with pytest.raises(ValueError, match="failed"):
        execution_complete("positive", {}, "Failed")
