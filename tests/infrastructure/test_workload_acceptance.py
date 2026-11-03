"""Real-cluster fixture acceptance retains immutable inputs and private transport boundaries."""

import copy
import json

import pytest
import workload_acceptance as acceptance
import yaml
from common import ROOT
from image_release import guard_workloads


@pytest.mark.skipif(
    not (ROOT / "artifacts/tools/bin/helm").is_file(),
    reason="Requires pinned Helm; exercised by infrastructure CI after tool installation",
)
def test_stub_chart_is_private_and_rejects_unpinned_images():
    documents = list(
        yaml.safe_load_all(acceptance.render_stub("example.invalid/stub@sha256:" + "a" * 64))
    )
    guard_workloads(documents)
    by_kind = {d["kind"]: d for d in documents}
    deployment, service, policy = (by_kind[k] for k in ("Deployment", "Service", "NetworkPolicy"))
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
    assert service["spec"]["type"] == "ClusterIP"
    assert policy["spec"]["egress"] == []
    assert policy["spec"]["ingress"][0]["from"] == [
        {"podSelector": {"matchLabels": {"app.kubernetes.io/managed-by": "klip"}}}
    ]
    with pytest.raises(RuntimeError, match="immutable"):
        acceptance.render_stub("example.invalid/stub:latest")


def test_preparation_preserves_all_seven_paths_and_never_retries_or_downloads(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(acceptance, "state_home", lambda: tmp_path)
    job = acceptance.preparation_job("abcdef012345", "example.invalid/runner@sha256:" + "b" * 64)
    acceptance.validate_preparation(job, job)
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0 and pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert not pod.get("initContainers")
    program = pod["containers"][0]["command"][2]
    compile(program, "preparation", "exec")
    assert all(s in program for s in acceptance.SCENARIOS)
    assert "'synthetic'" in program and "'--smoke'" in program
    assert "public" not in program and "password" not in program
    assert "/artifacts/acceptance/abcdef012345" in program
    for change in ("image", "command", "volume", "identity", "token", "privileged", "init"):
        altered = copy.deepcopy(job)
        p = altered["spec"]["template"]["spec"]
        if change == "image":
            p["containers"][0]["image"] += "-changed"
        elif change == "command":
            p["containers"][0]["command"] = ["unrelated"]
        elif change == "volume":
            p["volumes"][0]["persistentVolumeClaim"]["claimName"] = "another-claim"
        elif change == "identity":
            altered["metadata"]["labels"]["klip.dev/acceptance"] = "unrelated"
        elif change == "token":
            p["automountServiceAccountToken"] = True
        elif change == "privileged":
            p["containers"][0]["securityContext"]["privileged"] = True
        else:
            p["initContainers"] = [{"name": "unrelated"}]
        with pytest.raises(ValueError):
            acceptance.validate_preparation(altered, job)
    assert "secret" not in json.dumps(job).lower()
    with pytest.raises(ValueError, match="identity"):
        acceptance.preparation_job("../escape", "unused")
