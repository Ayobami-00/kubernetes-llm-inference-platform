"""Readiness counts native Deployment replicas rather than shared network labels."""

from copy import deepcopy

from management_acceptance import deployment_pods
from test_management_api_failure import allocation


def test_setup_jobs_and_foreign_deployments_cannot_replace_application_replicas():
    deployment, pods, replicasets = allocation()
    original = deepcopy(pods)
    pods.append(
        {
            "metadata": {
                "name": "setup-job",
                "ownerReferences": [{"kind": "Job", "uid": "synthetic-setup", "controller": True}],
            }
        }
    )
    foreign = deepcopy(original[0])
    foreign["metadata"]["ownerReferences"][0]["uid"] = "foreign-rs"
    pods.append(foreign)
    replicasets.append(
        {
            "metadata": {
                "uid": "foreign-rs",
                "ownerReferences": [
                    {"kind": "Deployment", "uid": "foreign-deployment", "controller": True}
                ],
            }
        }
    )
    assert deployment_pods(deployment, pods, replicasets) == original
    pods[0]["metadata"]["deletionTimestamp"] = "synthetic-terminating"
    assert deployment_pods(deployment, pods, replicasets) == [original[1]]
