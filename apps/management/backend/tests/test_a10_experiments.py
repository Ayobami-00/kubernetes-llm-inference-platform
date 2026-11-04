"""Management owns the durable pair; ordinary external resource mutation stays forbidden."""

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from platform_contracts import OperationStatus, RunSnapshot
from platform_management.api.routers import experiments
from platform_management.db.models import Operation, Resource, RunRow
from platform_management.db.session import session
from platform_management.services.a10_experiments import assert_run_access
from platform_management.services.common import AppError, digest
from sqlalchemy import select
from workload_runner.experiments.a10 import DEPLOYMENT


@pytest.fixture
def fixed_preset(monkeypatch):
    frozen = {
        "schema_version": "a10-paired.v1",
        "summary": {"name": "Test-only fixed preset"},
        "deployment_uid": "synthetic",
        "risk_reviews": {},
    }
    monkeypatch.setattr(experiments, "definition", lambda db: copy.deepcopy(frozen))
    with session() as db, db.begin():
        db.add(Resource(id=DEPLOYMENT, kind="deployment", payload={"ownership": "external"}))
    return frozen


def launch(client, frozen, key="a10-test-launch"):
    return client.post(
        "/api/v1/experiments/a10",
        json={"definition_sha256": digest(frozen)},
        headers={"Idempotency-Key": key},
    )


def test_pair_launch_reservation_history_and_terminal_replay(client, fixed_preset, monkeypatch):
    first = launch(client, fixed_preset)
    assert first.status_code == 202, first.text
    identity = first.json()["resource_id"]
    row = client.get("/api/v1/experiments/" + identity).json()
    assert row["status"] == "queued" and "definition" not in row
    assert client.get("/api/v1/experiments").json()[0]["id"] == identity
    with session() as db, db.begin():
        owner = db.get(Resource, DEPLOYMENT)
        assert owner.active_operation_id == first.json()["operation_id"]
        operation = OperationStatus.model_validate(
            db.get(Operation, owner.active_operation_id).payload
        )
        assert operation.target.resource_type == "experiment"
        parent = db.get(Resource, identity)
        parent.payload = {**parent.payload, "status": "completed"}
    monkeypatch.setattr(
        experiments,
        "definition",
        lambda db: (_ for _ in ()).throw(
            AssertionError("Terminal replay must not recheck credentials/readiness")
        ),
    )
    assert launch(client, fixed_preset).json() == first.json()
    assert (
        client.post(
            "/api/v1/experiments/a10",
            json={"definition_sha256": "0" * 64},
            headers={"Idempotency-Key": "a10-test-launch"},
        ).status_code
        == 409
    )


def test_concurrent_launch_is_one_parent_and_second_pair_is_rejected(client, fixed_preset):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: launch(client, fixed_preset), range(2)))
    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    assert launch(client, fixed_preset, key="a10-separate-launch").status_code == 409
    with session() as db:
        assert len(list(db.scalars(select(Resource).where(Resource.kind == "experiment")))) == 1


def test_cancel_is_durable_and_csrf_protected(client, fixed_preset):
    accepted = launch(client, fixed_preset).json()
    url = "/api/v1/experiments/" + accepted["resource_id"] + "/stop"
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(url, headers={"Idempotency-Key": "a10-stop-test"}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    stopped = client.post(url, headers={"Idempotency-Key": "a10-stop-test"})
    assert stopped.status_code == 202, stopped.text
    assert client.post(url, headers={"Idempotency-Key": "a10-stop-test"}).json() == stopped.json()
    assert client.get("/api/v1/experiments/" + accepted["resource_id"]).json()["stop_requested"]


def test_general_runs_cannot_enter_reserved_deployment(client, fixed_preset):
    accepted = launch(client, fixed_preset).json()
    with session() as db, db.begin():
        source = db.scalar(select(RunRow))
        snapshot = RunSnapshot.model_validate(source.snapshot)
        binding = snapshot.bindings[0].model_copy(update={"deployment_id": DEPLOYMENT})
        snapshot = snapshot.model_copy(update={"bindings": (binding, *snapshot.bindings[1:])})
        with pytest.raises(AppError, match="reserved"):
            assert_run_access(db, snapshot)
        assert_run_access(db, snapshot, accepted["operation_id"])


def test_ui_cannot_inject_arbitrary_serving_controls(client, fixed_preset):
    response = client.post(
        "/api/v1/experiments/a10",
        json={"definition_sha256": digest(fixed_preset), "image": "arbitrary:latest"},
        headers={"Idempotency-Key": "a10-invalid-controls"},
    )
    assert response.status_code == 422


def test_recovery_observes_committed_child_without_probe_or_replanning(
    client, fixed_preset, monkeypatch
):
    from platform_management.jobs import a10_experiment

    accepted = launch(client, fixed_preset).json()
    with session() as db, db.begin():
        parent = db.get(Resource, accepted["resource_id"])
        child = db.scalar(select(RunRow))
        child.context = {**child.context, "parent_experiment": parent.id}
        parent.payload = {**parent.payload, "children": {"baseline": child.id}}
        data = copy.deepcopy(parent.payload)
    # Recovery must return before interpreting any new input/configuration.
    assert a10_experiment.start_child(data, "baseline", {}, {}, None) == child.id
    data["definition"].update(deployment_uid="synthetic", risk_reviews={})

    class Control:
        def __init__(self, *args):
            pass

        def close(self):
            pass

    monkeypatch.setattr(a10_experiment, "A10Serving", Control)
    result = a10_experiment.step_experiment(
        {"id": accepted["operation_id"], "type": "start_experiment"},
        data,
        {"step": "baseline-ready"},
        {},
    )
    assert result[1]["step"] == "baseline-run"
    assert result[0]["children"]["baseline"] == child.id
    assert result[2] is False


def test_startup_cancellation_releases_reservation_without_serving_activation(
    client, fixed_preset, monkeypatch, tmp_path
):
    from platform_management.jobs import a10_experiment
    from platform_management.settings import get_settings

    monkeypatch.setattr(get_settings(), "artifacts_root", tmp_path)

    accepted = launch(client, fixed_preset).json()
    with session() as db, db.begin():
        parent = db.get(Resource, accepted["resource_id"])
        parent.payload = {**parent.payload, "stop_requested": True}
        data = copy.deepcopy(parent.payload)
    data["definition"]["deployment_uid"] = "synthetic"
    released = []

    class Control:
        def __init__(self, *args):
            pass

        def release(self, owner):
            released.append(owner)

        def close(self):
            pass

    monkeypatch.setattr(a10_experiment, "A10Serving", Control)
    result = a10_experiment.step_experiment(
        {"id": accepted["operation_id"], "type": "start_experiment"}, data, {}, {}
    )
    assert result[0]["status"] == "cancelled" and result[2] is True
    assert released == [accepted["operation_id"]]
    with session() as db:
        assert db.get(Resource, DEPLOYMENT).active_operation_id is None
