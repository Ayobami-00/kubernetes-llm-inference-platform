"""Retained-host onboarding never acquires provider lifecycle ownership."""

import ast
import json
import re

import pytest
import retained_gpu as retained


@pytest.mark.parametrize("previous", [None, "a" * 64])
def test_native_guards_survive_python_optimization(previous):
    program = retained.native_program(previous)
    tree = ast.parse(program)
    assert not any(isinstance(n, ast.Assert) for n in ast.walk(tree))
    require = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "require")
    namespace = {}
    exec(
        compile(ast.Module(body=[require], type_ignores=[]), "retained-guard", "exec", optimize=2),
        namespace,
    )
    with pytest.raises(ValueError):
        namespace["require"](False, "invalid identity")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fullmatch"
        ):
            assert re.fullmatch(node.args[0].value, "abc123.0123456789abcdef")
    assert "terminate" not in program and "reboot" not in program
    assert (
        "LOCK_NB" in program and "dispatch.lock" in program and "cluster/operator.lock" in program
    )
    if previous:
        assert "Prior peer differs" in program
        assert "expected_names.add(name)" in program


def test_retained_job_is_single_attempt_with_writable_ansible_state(monkeypatch):
    import host_executor_release

    monkeypatch.setattr(host_executor_release, "guard_workloads", lambda _: None)
    deployment = {
        "kind": "Deployment",
        "spec": {
            "template": {
                "metadata": {},
                "spec": {"containers": [{"env": [], "volumeMounts": []}], "volumes": []},
            }
        },
    }
    job = retained.join_job({"retained": {"instance_id": retained.INSTANCE}}, deployment)
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert {"name": "ansible", "mountPath": "/app/artifacts/ansible"} in container["volumeMounts"]
    assert container["command"] == ["python", "-c", retained.JOIN_PROGRAM]
    inputs = next(e["value"] for e in container["env"] if e["name"] == "KLIP_RETAINED_INPUT")
    assert json.loads(inputs)["retained"]["instance_id"] == retained.INSTANCE
    assert not any("lease" in e["name"].lower() for e in container["env"])


def test_recovery_requires_an_exact_prior_result():
    with pytest.raises(ValueError, match="checksum"):
        retained.native_program("latest")
