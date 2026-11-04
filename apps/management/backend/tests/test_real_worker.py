"""Durable real-adapter workflows with simulated external I/O, never paid resources."""

import base64
import copy
import hashlib
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from platform_contracts import HostTaskObservation, WorkerGroup
from platform_management.db.models import Job, ProviderIntent, Resource, Revision, Spec
from platform_management.db.session import session
from platform_management.integrations import registry
from platform_management.integrations.host_tasks import task_digest
from platform_management.integrations.lambda_policy import LambdaLifecyclePolicy
from platform_management.jobs.real_worker import step_real_group
from platform_management.jobs.worker import counts
from platform_management.providers.base import AllocationAmbiguous
from platform_management.services.common import AppError, now
from test_management import post, settle

INSTANCE = "a" * 32
NODE_UID = "12345678-1234-1234-1234-123456789abc"
HOST_KEY = (
    "ssh-ed25519 "
    + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode()
)
CONTENT = "#cloud-config\nruncmd: []\n"


def policy():
    return LambdaLifecyclePolicy(
        kube_system_uid=NODE_UID,
        image_id="87654321-4321-4321-4321-cba987654321",
        ssh_key_name="klip-synthetic-key",
        ssh_public_key_sha256=hashlib.sha256(HOST_KEY.encode()).hexdigest(),
        firewall_ruleset_ids=("11111111-2222-3333-4444-555555555555",),
    )


def binding():
    return {
        **registry.configuration("real"),
        "provider": "lambda:v1",
        "host": "isolated-host:v1",
        "cluster": "isolated-host:v1",
        "lifecycle": {"policy": policy().model_dump(mode="json")},
    }


def initial():
    with session() as db:
        spec = copy.deepcopy(db.get(Revision, "worker-group:latency-workers@1").document)
    spec.update(instance_type="gpu_1x_a10", region="us-east-1")
    group = WorkerGroup(
        fixture=False,
        spec=spec,
        status={
            "worker_group_ref": "latency-workers@1",
            "phase": "allocating",
            "counts": {
                "desired_instances": 1,
                "allocated_instances": 0,
                "joined_nodes": 0,
                "gpu_ready_nodes": 0,
            },
            "observed_at": now(),
        },
    )
    return {
        "id": "real-test-group",
        "mode": "real",
        "ownership": "management",
        "fault": "none",
        "group": group.model_dump(mode="json"),
    }


class FakeIO:
    def __init__(self):
        self.plan, self.instance_value = None, None
        self.launches, self.terminations, self.termination_reads = 0, 0, 0
        self.host_calls, self.host_results, self.native_jobs = [], {}, set()
        self.fail_action, self.running_action = None, None
        self.lost_launch, self.visible, self.attest = False, True, True
        self.revision, self.joined = 12, False

    @contextmanager
    def provider(self):
        yield self

    def host(self):
        return self

    def close(self):
        pass

    def cluster(self):
        return {
            "schema_version": "klip-host-executor-metadata.v1",
            "cluster_id": "klip",
            "kube_system_uid": NODE_UID,
            "registry_revision": self.revision,
            "occupied_wireguard_ips": [f"10.200.0.{i}" for i in (11, 12, 13, 64, 65, 66)]
            + (["10.200.0.67"] if self.joined else []),
            "bootstrap_public_key": HOST_KEY,
            "gpu_inspection_configured": True,
            "gpu_runtime_review_sha256": "f" * 64,
        }

    def cloud_init(self, identity):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, identity, with_for_update=True)
            row.payload = {
                **row.payload,
                "bootstrap": {
                    "expires_at": (
                        now() + timedelta(minutes=20)
                        if self.attest
                        else now() - timedelta(seconds=1)
                    ).isoformat(),
                    **({"host_key": HOST_KEY} if self.attest else {}),
                },
            }
        return CONTENT, hashlib.sha256(CONTENT.encode()).hexdigest()

    def check_offering(self, plan, ceiling, *, image_version=None):
        assert ceiling == 129
        assert image_version == policy().image_version
        return {"instance_type": plan.instance_type, "price_cents_per_hour": 129}

    def launch(self, first, second=None):
        if second is not None:
            plan = first
            with session() as db:
                assert db.get(ProviderIntent, plan.intent_id).state == "dispatching"
            self.launches += 1
            self.plan = plan
            self.instance_value = {
                "id": INSTANCE,
                "name": plan.name,
                "tags": [{"key": k, "value": v} for k, v in plan.tags.items()],
                "instance_type": {"name": plan.instance_type},
                "region": {"name": plan.region},
                "image": {"id": plan.image_id},
                "ssh_key_names": [plan.ssh_key_name],
                "status": "active",
                "ip": "8.8.8.8",
                "private_ip": "192.168.1.2",
            }
            if self.lost_launch:
                raise AllocationAmbiguous("Synthetic lost provider response")
            return (INSTANCE,)
        task = first
        self.host_calls.append(task)
        if task.id in self.host_results:
            return self.host_results[task.id]
        self.native_jobs.add(task.id)
        common = dict(
            task_id=task.id,
            input_sha256=task_digest(task),
            job_uid="native-" + task.id,
            observed_at=now(),
            registry_revision=self.revision,
        )
        if task.action == self.running_action:
            return HostTaskObservation(**common, status="running", stage="Synthetic long host task")
        if task.action == self.fail_action:
            result = HostTaskObservation(
                **common,
                status="failed",
                stage="Synthetic host failure",
                cleanup_pending=True,
                error={"code": "test_host_failure", "message": "Controlled external failure"},
            )
        else:
            if task.action == "join":
                self.joined = True
                self.revision += 1
            elif task.action in {"remove", "revoke-bootstrap"}:
                self.joined = False
                self.revision += 1
            known_uid = task.node_uid or (NODE_UID if self.joined else None)
            ready = task.action in {"join", "inspect"}
            result = HostTaskObservation(
                **{**common, "registry_revision": self.revision},
                status="completed",
                stage="Synthetic host completion",
                node_name=task.node_name if known_uid else None,
                node_uid=known_uid,
                public_wireguard_key=base64.b64encode(b"w" * 32).decode()
                if task.action == "prepare"
                else None,
                host_ready=task.action in {"prepare", "join", "inspect"},
                wireguard_ready=ready,
                kubernetes_ready=ready,
                cilium_ready=ready,
                connectivity_verified=task.action == "inspect",
                gpu_canary_verified=task.action == "inspect",
                gpu_runtime_ready=task.action == "inspect",
                gpu_observation={
                    "observed_at": common["observed_at"],
                    "allocatable_gpu_count": 1,
                    "devices": [
                        {
                            "index": 0,
                            "uuid": "GPU-11111111-2222-3333-4444-555555555555",
                            "name": "NVIDIA A10",
                            "memory_total_mib": 23028,
                            "driver_version": "595.91.07",
                        }
                    ],
                }
                if task.action == "inspect"
                else None,
                evidence_sha256="b" * 64,
                bootstrap_fence_permitted=task.action == "cleanup-check",
                bootstrap_cleanup_complete=task.action == "revoke-bootstrap",
            )
        self.host_results[task.id] = result
        return result

    def observe(self, identity):
        task = next(task for task in self.host_calls if task.id == identity)
        return self.launch(task)

    def instances(self):
        if self.instance_value and self.instance_value["status"] == "terminating":
            self.termination_reads += 1
            if self.termination_reads >= 2:
                self.instance_value["status"] = "terminated"
        return [copy.deepcopy(self.instance_value)] if self.instance_value and self.visible else []

    def instance(self, identity):
        assert identity == INSTANCE
        return copy.deepcopy(self.instance_value)

    def terminate(self, identity):
        assert identity == INSTANCE
        self.terminations += 1
        self.instance_value["status"] = "terminating"
        return [copy.deepcopy(self.instance_value)]


def advance(io, payload=None, *, op=None, checkpoint=None, until=None, limit=35):
    payload = payload or initial()
    op = op or {"id": "op-" + "1" * 16, "type": "create_worker_group"}
    checkpoint = checkpoint or {"request": {}}
    for _ in range(limit):
        result = step_real_group(op, payload, checkpoint, binding(), io=io)
        payload, checkpoint, done, _, _ = result
        counts(payload["group"])
        if done or (until and checkpoint.get("step") == until):
            return result
    raise AssertionError("Synthetic external workflow did not reach its expected boundary")


def test_create_reclaimed_steps_renew_and_normal_delete_preserve_real_receipts():
    io = FakeIO()
    payload, checkpoint, done, failed, _ = advance(io)
    assert done and not failed and payload["group"]["status"]["phase"] == "ready"
    assert io.launches == 1 and len(io.native_jobs) == 3
    assert payload["group"]["status"]["counts"]["gpu_ready_nodes"] == 1
    renewal = {"id": "op-" + "2" * 16, "type": "renew_worker_group"}
    renewed = advance(io, payload, op=renewal, checkpoint={"request": {"additional_minutes": 30}})
    replay = advance(io, renewed[0], op=renewal, checkpoint={"request": {"additional_minutes": 30}})
    assert replay[0]["group"]["status"]["expires_at"] == renewed[0]["group"]["status"]["expires_at"]
    removed = advance(io, renewed[0], op={"id": "op-" + "3" * 16, "type": "delete_worker_group"})
    assert removed[0]["group"]["status"]["phase"] == "deleted"
    assert io.terminations == 1 and io.launches == 1 and len(io.native_jobs) == 4
    assert io.host_calls[-1].action == "remove"
    with session() as db:
        value = db.get(ProviderIntent, "op-" + "1" * 16).payload["lambda_launch"]
        assert (
            value["state"] == "terminated"
            and value["termination_authorization"]["route"] == "drained"
        )


@pytest.mark.parametrize("action", ["prepare", "join", "inspect"])
def test_failed_bootstrap_cleans_up_without_claiming_gpu_ready(action):
    io = FakeIO()
    io.fail_action = action
    payload, checkpoint, done, failed, _ = advance(io)
    assert done and failed and payload["group"]["status"]["phase"] == "failed"
    assert payload["group"]["status"]["counts"]["allocated_instances"] == 0
    assert (
        checkpoint["failure"]["code"]
        == {
            "prepare": "host_prepare_failed",
            "join": "kubernetes_join_failed",
            "inspect": "gpu_readiness_failed",
        }[action]
    )
    assert io.launches == 1 and io.terminations == 1
    actions = [task.action for task in io.host_calls]
    if action == "inspect":
        assert "remove" in actions
    else:
        assert "cleanup-check" in actions and "revoke-bootstrap" in actions


def test_lost_launch_response_reconciles_without_second_rental():
    io = FakeIO()
    io.lost_launch = True
    result = advance(io)
    assert result[0]["group"]["status"]["phase"] == "ready" and io.launches == 1


def test_failed_attestation_terminates_without_inventing_host_identity():
    io = FakeIO()
    io.attest = False
    result = advance(io)
    assert result[2] and result[3] and io.launches == 1 and io.terminations == 1
    assert not io.host_calls and not io.native_jobs


def test_drain_failure_never_terminates_provider_and_preserves_reservation():
    io = FakeIO()
    created = advance(io)
    io.fail_action = "remove"
    with pytest.raises(AppError, match="provider remains allocated"):
        advance(io, created[0], op={"id": "op-" + "2" * 16, "type": "delete_worker_group"})
    assert io.terminations == 0
    with session() as db:
        assert (
            db.get(ProviderIntent, "op-" + "1" * 16).payload["lambda_launch"]["state"]
            == "allocated"
        )


def test_pending_host_observations_do_not_consume_generic_exception_retries():
    io = FakeIO()
    io.running_action = "prepare"
    result = advance(io, until="prepare")
    for _ in range(7):
        result = step_real_group(
            {"id": "op-" + "1" * 16, "type": "create_worker_group"},
            result[0],
            result[1],
            binding(),
            io=io,
        )
        assert not result[2] and not result[3]
    assert len(io.native_jobs) == 1 and io.launches == 1
    io.running_action = None
    assert advance(io, result[0], checkpoint=result[1])[2]


def test_real_api_queue_runs_actual_branch_and_reuses_terminal_submission(client, monkeypatch):
    from platform_management.jobs import real_worker

    io, frozen = FakeIO(), binding()
    original = registry.configuration
    monkeypatch.setattr(
        registry,
        "configuration",
        lambda mode="simulation": copy.deepcopy(frozen) if mode == "real" else original(mode),
    )
    monkeypatch.setattr(real_worker, "RuntimeIO", lambda _: io)
    with session() as db, db.begin():
        base = copy.deepcopy(db.get(Revision, "worker-group:latency-workers@1").document)
        base.update(revision=2, instance_type="gpu_1x_a10", region="us-east-1")
        db.add(
            Revision(
                key="worker-group:latency-workers@2",
                spec_key="worker-group:latency-workers",
                revision=2,
                document=base,
            )
        )
        db.get(Spec, "worker-group:latency-workers").current_revision = 2
    key = uuid4().hex
    body = {"id": "real-api-worker", "spec_ref": "latency-workers@2", "mode": "real"}
    created = post(client, "/worker-groups", body, key)
    assert created.status_code == 202, created.text
    options = client.get("/api/v1/worker-lifecycle-options").json()
    assert options["configured"] is True
    assert options["compatible_specs"] == [
        {"spec_ref": "latency-workers@2", "worker_class": "latency", "lease_minutes": 90}
    ]
    assert "tls_fingerprints" not in str(options) and "bootstrap_seed_file" not in str(options)
    settle(60)
    with session() as db:
        resource = db.get(Resource, body["id"])
        assert resource.payload["group"]["status"]["phase"] == "ready"
        assert not resource.payload["group"]["fixture"]
        assert db.get(Job, created.json()["operation_id"]).state == "completed"
    assert post(client, "/worker-groups", body, key).json() == created.json()
    excessive_renewal = post(
        client, "/worker-groups/real-api-worker/renew", {"additional_minutes": 91}
    )
    assert excessive_renewal.status_code == 422
    with session() as db:
        assert db.get(Resource, body["id"]).payload["group"]["status"]["phase"] == "ready"
    simulated = post(
        client,
        "/serving-deployments",
        {
            "id": "wrong-mode-serving",
            "profile_ref": "gec-baseline@1",
            "group_id": body["id"],
        },
    )
    assert simulated.status_code == 422 and "modes must match" in simulated.text
    deleted = post(client, "/worker-groups/real-api-worker/delete")
    assert deleted.status_code == 202
    settle(60)
    with session() as db:
        assert db.get(Resource, body["id"]).payload["group"]["status"]["phase"] == "deleted"
    assert io.launches == 1 and io.terminations == 1


def test_disabled_live_options_do_not_expose_secrets_or_accept_a_real_allocation(client):
    response = client.get("/api/v1/worker-lifecycle-options")
    assert response.status_code == 200
    options = response.json()
    assert options["configured"] is False and options["compatible_specs"] == []
    assert "lambda_api_key" not in response.text and "tls" not in response.text
    denied = post(
        client,
        "/worker-groups",
        {"id": "disabled-real", "spec_ref": "latency-workers@1", "mode": "real"},
    )
    assert denied.status_code == 422
    with session() as db:
        assert db.get(Resource, "disabled-real") is None


def test_unknown_launch_deadline_never_recreates_the_vm_or_claims_cleanup():
    io = FakeIO()
    io.lost_launch, io.visible = True, False
    op = {"id": "op-" + "1" * 16, "type": "create_worker_group"}
    result = step_real_group(op, initial(), {"request": {}}, binding(), io=io)
    assert not result[2] and io.launches == 1
    result[1]["workflow_deadline"] = (now() - timedelta(seconds=1)).isoformat()
    with pytest.raises(AppError, match="deadline reached"):
        step_real_group(op, result[0], result[1], binding(), io=io)
    with session() as db:
        allocation = db.get(ProviderIntent, op["id"]).payload["lambda_launch"]
        assert allocation["state"] == "dispatching" and allocation["instance_ids"] == []
    assert io.terminations == 0 and not io.native_jobs


def test_committed_allocation_survives_lost_resource_checkpoint_and_can_be_deleted(
    client, monkeypatch
):
    from platform_management.jobs import real_worker
    from platform_management.providers.lambda_mutations import ProviderIdentityConflict

    io, frozen = FakeIO(), binding()
    original_configuration, original_step = registry.configuration, real_worker.step_real_group
    monkeypatch.setattr(
        registry,
        "configuration",
        lambda mode="simulation": (
            copy.deepcopy(frozen) if mode == "real" else original_configuration(mode)
        ),
    )
    monkeypatch.setattr(real_worker, "RuntimeIO", lambda _: io)
    with session() as db, db.begin():
        document = copy.deepcopy(db.get(Revision, "worker-group:latency-workers@1").document)
        document.update(revision=2, instance_type="gpu_1x_a10", region="us-east-1")
        db.add(
            Revision(
                key="worker-group:latency-workers@2",
                spec_key="worker-group:latency-workers",
                revision=2,
                document=document,
            )
        )

    def lose_checkpoint(op, *args, **kwargs):
        result = original_step(op, *args, **kwargs)
        if op["type"] == "create_worker_group":
            raise ProviderIdentityConflict("Synthetic failure after committed allocation")
        return result

    monkeypatch.setattr(real_worker, "step_real_group", lose_checkpoint)
    created = post(
        client,
        "/worker-groups",
        {"id": "lost-checkpoint", "spec_ref": "latency-workers@2", "mode": "real"},
    )
    assert created.status_code == 202
    settle(60)
    with session() as db:
        resource = db.get(Resource, "lost-checkpoint")
        assert resource.payload["group"]["status"]["counts"]["allocated_instances"] == 1
        assert resource.payload["group"]["status"]["phase"] == "draining"
        assert resource.active_operation_id is None
    assert post(client, "/worker-groups/lost-checkpoint/delete").status_code == 202
    settle(60)
    with session() as db:
        assert db.get(Resource, "lost-checkpoint").payload["group"]["status"]["phase"] == "deleted"
    assert io.launches == 1 and io.terminations == 1 and not io.native_jobs
