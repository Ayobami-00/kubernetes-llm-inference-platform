"""Catch chart defaults that controller dry-runs do not subject to Pod admission."""

import copy

import pytest
from observability_release import PLACEMENT, check_render


def safe_statefulset():
    return {
        "kind": "StatefulSet",
        "spec": {
            "template": {
                "spec": {
                    "nodeSelector": PLACEMENT,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "synthetic",
                            "securityContext": {
                                "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                }
            }
        },
    }


def test_stateful_chart_defaults_cannot_hide_missing_seccomp():
    document = safe_statefulset()
    check_render([document])
    document["spec"]["template"]["spec"]["securityContext"] = {"fsGroup": 1000}
    with pytest.raises(ValueError, match="seccomp"):
        check_render([document])


@pytest.mark.parametrize(
    "change",
    [
        {"privileged": True},
        {"allowPrivilegeEscalation": True},
        {"capabilities": {"drop": []}},
        {"readOnlyRootFilesystem": False},
        {"runAsUser": 0},
    ],
)
def test_every_container_including_init_must_keep_the_boundary(change):
    document = safe_statefulset()
    pod = document["spec"]["template"]["spec"]
    init = copy.deepcopy(pod["containers"][0])
    init["name"] = "synthetic-init"
    init["securityContext"].update(change)
    pod["initContainers"] = [init]
    with pytest.raises(ValueError):
        check_render([document])
