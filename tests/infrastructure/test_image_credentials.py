"""Reject host drift and keep image credentials scoped to Pod identities."""

from copy import deepcopy

import pytest
from image_credentials import BASE_ARGS, PATHS, assert_host, provider_config


def host():
    return {
        "version": "Kubernetes v1.36.5",
        "argv": BASE_ARGS,
        "drop_ins": ["/usr/lib/systemd/system/kubelet.service.d/10-kubeadm.conf"],
        "kubelet_config": "apiVersion: kubelet.config.k8s.io/v1beta1\n",
        "files": {PATHS[0]: None, "/var/lib/kubelet/config.yaml": {"uid": 0, "mode": 0o600}},
    }


def test_helper_has_no_host_credential_fallback_or_unscoped_registries():
    provider = provider_config()["providers"][0]
    assert len(provider["matchImages"]) == 3
    assert all(
        x.startswith("127811062476.dkr.ecr.eu-west-2.amazonaws.com/klip/") and "*" not in x
        for x in provider["matchImages"]
    )
    env = {v["name"]: v["value"] for v in provider["env"]}
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == env["AWS_CONFIG_FILE"] == "/dev/null"
    assert provider["tokenAttributes"] == {
        "serviceAccountTokenAudience": "sts.amazonaws.com",
        "cacheType": "Token",
        "requireServiceAccount": True,
        "requiredServiceAccountAnnotationKeys": ["eks.amazonaws.com/ecr-role-arn"],
    }


@pytest.mark.parametrize(
    "change,match",
    [
        ({"version": "Kubernetes v1.35.0"}, "release/ExecStart"),
        ({"argv": BASE_ARGS + " --custom-override=unsafe"}, "release/ExecStart"),
        ({"drop_ins": ["/etc/systemd/system/kubelet.service.d/99-other.conf"]}, "Unreviewed"),
        (
            {
                "kubelet_config": "featureGates:\n"
                "  KubeletServiceAccountTokenForCredentialProviders: false\n"
            },
            "disabled",
        ),
    ],
)
def test_host_overrides_cannot_be_silently_replaced(change, match):
    review = {**host(), **change}
    with pytest.raises(ValueError, match=match):
        assert_host(review)


def test_root_owned_immutable_inputs_and_known_installed_unit_are_supported():
    review = host()
    assert_host(review)
    review["argv"] += " $KUBELET_IMAGE_CREDENTIAL_ARGS"
    review["drop_ins"].append(PATHS[2])
    assert_host(review)
    altered = deepcopy(review)
    altered["files"]["/var/lib/kubelet/config.yaml"]["mode"] = 0o666
    with pytest.raises(ValueError, match="writable"):
        assert_host(altered)
