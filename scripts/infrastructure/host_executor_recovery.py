"""Read-only proof for an image repair while a never-joined VM is already fenced."""


def fenced_chain(root, identity):
    # Keep this function self-contained: operator preview runs its exact source
    # in the current executor, and the upgrade guard repeats it under both locks.
    import hashlib
    import json
    import re
    from datetime import UTC, datetime

    from platform_contracts import HostTask, HostTaskObservation
    from platform_contracts.host_operations import validate_host_followup
    from platform_management.host_executor.controller import canonical

    if not re.fullmatch(r"host-[a-f0-9]{24}", identity):
        raise ValueError("Exact fenced host task identity required")
    files, tasks, seen = {}, [], set()
    current, child = identity, None
    while current:
        if current in seen or len(seen) >= 100:
            raise ValueError("Invalid fenced task chain")
        seen.add(current)
        directory = root / "executions" / current
        names = ["task.json", "observation.json", "receipt.json", "job-uid"]
        retained = {name: (directory / name).read_bytes() for name in names}
        task = HostTask.model_validate_json(retained["task.json"])
        observed = HostTaskObservation.model_validate_json(retained["observation.json"])
        receipt = json.loads(retained["receipt.json"])
        sha = hashlib.sha256(canonical(task)).hexdigest()
        if (
            task.id != current
            or observed.task_id != current
            or observed.input_sha256 != sha
            or receipt["manifest"]["metadata"]["annotations"]["klip.dev/input-sha256"] != sha
            or observed.job_uid != retained["job-uid"].decode()
            or receipt.get("job_uid") != observed.job_uid
            or observed.status not in {"completed", "failed"}
            or observed.observed_at > datetime.now(UTC)
            or task.node_uid is not None
            or observed.node_uid is not None
            or task.action not in {"prepare", "cleanup-check", "revoke-bootstrap"}
        ):
            raise ValueError("Recovery requires a terminal, UID-bound, never-joined task chain")
        if child:
            validate_host_followup(child, task, observed)
        else:
            if (
                task.action != "revoke-bootstrap"
                or task.provider_fence is None
                or task.provider_fence.confirmed_at > datetime.now(UTC)
                or not (
                    (observed.status == "failed" and observed.cleanup_pending)
                    or (observed.status == "completed" and observed.bootstrap_cleanup_complete)
                )
            ):
                raise ValueError(
                    "Recovery requires failed/finished revocation with a provider fence"
                )
            head = task
            active = root / "active-task.json"
            if observed.status == "failed" and json.loads(active.read_text())["id"] != identity:
                raise ValueError("A different host task owns the executor")
        for name, data in retained.items():
            files[str((directory / name).relative_to(root))] = hashlib.sha256(data).hexdigest()
        tasks.append(task.id)
        child, current = task, task.previous_task_id
    if task.action != "prepare":
        raise ValueError("Fenced recovery chain must begin with host preparation")
    return {
        "schema_version": "klip-fenced-image-recovery.v1",
        "head": identity,
        "provider_instance_id": head.provider_instance_id,
        "node_name": head.node_name,
        "provider_fence": head.provider_fence.model_dump(mode="json"),
        "tasks": tasks,
        "files_sha256": files,
    }


def preview(cp, identity):
    import inspect
    import json

    from cluster import kubectl

    pods = json.loads(
        kubectl(
            "-n",
            "platform-ops",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=klip-host-executor,app.kubernetes.io/component=api",
            "-o",
            "json",
        )
    )["items"]
    if len(pods) != 2:
        raise ValueError("Fenced recovery requires the existing HA executor")
    program = (
        "from pathlib import Path\nimport json\n"
        + inspect.getsource(fenced_chain)
        + "\nprint(json.dumps(fenced_chain(Path('/executor/state'), "
        + repr(identity)
        + ")))"
    )
    return json.loads(
        cp.run(
            [
                "sudo",
                "kubectl",
                "--kubeconfig=/etc/kubernetes/admin.conf",
                "-n",
                "platform-ops",
                "exec",
                pods[0]["metadata"]["name"],
                "-c",
                "api",
                "--",
                "python",
                "-c",
                program,
            ],
            secret=True,
        )
    )
