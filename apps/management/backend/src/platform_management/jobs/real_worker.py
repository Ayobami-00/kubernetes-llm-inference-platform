"""Real single-A10 workflow using C's ownership and the isolated host executor.

Each step runs outside C's transaction. Provider intents and host inputs commit
before external mutations, so a reclaimed management Job resumes the same work.
"""

import copy
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from platform_contracts import HostTask, HostTaskObservation, WorkerGroupSpec
from sqlalchemy import select

from platform_management.db.models import ProviderIntent
from platform_management.db.session import session
from platform_management.host_executor.client import HostExecutorClient
from platform_management.integrations.host_tasks import DurableHostTasks
from platform_management.integrations.lambda_policy import checked_binding, public_fingerprints
from platform_management.providers.base import AllocationAmbiguous, TransientProviderError
from platform_management.providers.lambda_ledger import (
    PostgresLaunchLedger,
    allocate_once,
    request_gate,
    terminate_once,
)
from platform_management.providers.lambda_mutations import (
    LambdaMutations,
    LaunchPlan,
    ProviderIdentityConflict,
    ProviderRejected,
    owned_instances,
)
from platform_management.services import bootstrap
from platform_management.services.common import AppError, digest, now


class RuntimeIO:
    def __init__(self, binding):
        self.binding, self.config = binding, binding["lifecycle"]

    @contextmanager
    def provider(self):
        key = Path(self.config["lambda_api_key_file"]).read_text().strip()
        if hashlib.sha256(key.encode()).hexdigest() != self.config["lambda_api_key_sha256"]:
            raise AppError(
                409,
                "Bound Lambda credential changed; reconcile the original account identity",
                "provider_identity_conflict",
            )
        api = LambdaMutations(key, gate=request_gate)
        try:
            yield api
        finally:
            api.close()

    def host(self):
        folder = Path(self.config["host_executor_tls_home"])
        if public_fingerprints(folder) != self.config["tls_fingerprints"]:
            raise AppError(409, "Bound executor TLS identity changed", "host_identity_conflict")
        return HostExecutorClient(folder)

    def cloud_init(self, identity):
        path = Path(self.config["bootstrap_seed_file"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.config["bootstrap_seed_sha256"]:
            raise AppError(409, "Bound bootstrap seed changed", "host_identity_conflict")
        return bootstrap.issue(identity, self.config["origin"], path)


class RealWorkerWorkflow:
    def __init__(self, op, payload, checkpoint, binding, io=None):
        self.op, self.payload, self.checkpoint = (
            op,
            copy.deepcopy(payload),
            copy.deepcopy(checkpoint),
        )
        self.policy, self.binding = checked_binding(binding), binding
        self.io, self.ledger = io or RuntimeIO(binding), PostgresLaunchLedger()
        self.group = self.payload["group"]
        self.state = self.group["status"]
        self.spec = WorkerGroupSpec.model_validate(self.group["spec"])
        self.policy.check_group(self.spec)
        if (
            self.payload.get("ownership", "management") != "management"
            or self.payload.get("mode") != "real"
            or self.group["fixture"]
        ):
            raise AppError(
                409,
                "Real lifecycle cannot mutate fixture or external resources",
                "external_ownership",
            )
        if self.payload.get("fault", "none") != "none":
            raise AppError(422, "Simulation faults are unavailable in real mode")
        self.plan = None

    def intent(self):
        with session() as db:
            rows = [
                row
                for row in db.scalars(
                    select(ProviderIntent).where(ProviderIntent.group_id == self.payload["id"])
                )
                if row.payload.get("mode") == "real"
            ]
            if len(rows) > 1:
                raise ProviderIdentityConflict("Worker group has multiple real allocation intents")
            return (
                copy.deepcopy(rows[0].payload) | {"id": rows[0].id, "state": rows[0].state}
                if rows
                else None
            )

    def metadata(self):
        client = self.io.host()
        try:
            metadata = client.cluster()
        finally:
            client.close()
        self.policy.check_metadata(metadata)
        return metadata

    def initialize(self):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, self.op["id"], with_for_update=True)
            expected = {"mode": "real", "spec_sha256": digest(self.group["spec"])}
            if row is None:
                db.add(
                    ProviderIntent(id=self.op["id"], group_id=self.payload["id"], payload=expected)
                )
            elif row.group_id != self.payload["id"] or any(
                row.payload.get(k) != v for k, v in expected.items()
            ):
                raise ProviderIdentityConflict("Original allocation intent changed")
        intent = self.intent()
        if intent.get("lambda_launch"):
            self.plan = LaunchPlan.model_validate(intent["lambda_launch"]["plan"])
            return None
        # Verify cluster/public bootstrap identity before authorizing paid dispatch.
        self.metadata()
        content, sha = self.io.cloud_init(self.op["id"])
        self.plan = LaunchPlan(
            intent_id=self.op["id"],
            instance_type=self.policy.instance_type,
            region=self.policy.region,
            image_id=self.policy.image_id,
            ssh_key_name=self.policy.ssh_key_name,
            firewall_ruleset_ids=self.policy.firewall_ruleset_ids,
            lease_minutes=self.spec.ttl_minutes,
            user_data_sha256=sha,
        )
        self.ledger.freeze(self.plan)
        return content

    def sync(self):
        value = self.ledger.read(self.plan)
        if value.get("dispatched_at"):
            self.state.update(allocated_at=value["dispatched_at"], expires_at=value["expires_at"])
        previous = {m["provider_instance_id"]: m for m in self.state["members"]}
        self.state["members"] = [
            previous.get(
                identity,
                {
                    "provider_instance_id": identity,
                    "phase": "allocated",
                    "node_name": None,
                    "node_uid": None,
                    "error": None,
                },
            )
            for identity in value["instance_ids"]
        ]
        if value["state"] == "terminated":
            for member in self.state["members"]:
                member.update(phase="terminated", error=None)
        return value

    def failed(self, code, message):
        self.checkpoint.update(
            step="cleanup", creation_failed=True, failure={"code": code, "message": message}
        )
        self.state["phase"] = "draining"
        for member in self.state["members"]:
            member.update(phase="failed", error=self.checkpoint["failure"])

    def task(self, action, *, target=None):
        store = DurableHostTasks(self.plan, self.io.host)
        slot = self.op["id"] + ":" + action
        identity = "host-" + digest([self.plan.intent_id, slot])[:24]
        current = self.ledger.read(self.plan)
        if identity in current["host_tasks"]:
            return store, HostTask.model_validate(current["host_tasks"][identity]["task"])
        metadata = self.metadata()
        if action == "prepare":
            self.ledger.reserve_wireguard(self.plan, metadata)

        def build(identity, value):
            previous_id = value["active_host_task_id"]
            prior = value["host_tasks"].get(previous_id)
            if prior:
                previous = HostTask.model_validate(prior["task"])
                observed = prior.get("observation") or {}
                fields = {
                    key: getattr(previous, key)
                    for key in (
                        "provider_instance_id",
                        "node_name",
                        "public_ip",
                        "private_ip",
                        "wireguard_ip",
                        "trusted_host_key",
                    )
                }
                fields["node_uid"] = observed.get("node_uid") or previous.node_uid
            else:
                if target is None:
                    raise ProviderIdentityConflict(
                        "Initial host task has no attested provider target"
                    )
                fields = {**target, "wireguard_ip": value["wireguard_reservation"]["address"]}
            deadline = now() + timedelta(seconds=self.policy.host_timeout_s)
            if action in {"prepare", "join", "inspect"}:
                deadline = min(
                    deadline,
                    datetime.fromisoformat(value["expires_at"])
                    - timedelta(seconds=self.policy.cleanup_margin_s),
                )
            return HostTask(
                id=identity,
                action=action,
                **fields,
                registry_revision=metadata["registry_revision"],
                previous_task_id=previous_id,
                deadline=deadline,
                drain_timeout_s=self.spec.drain_timeout_s,
                provider_fence=value.get("provider_fence")
                if action == "revoke-bootstrap"
                else None,
            )

        return store, store.ensure(slot, build)

    def host_step(self, action, *, target=None):
        store, task = self.task(action, target=target)
        observed = store.launch(task)
        if observed.node_uid:
            self.state["members"][0].update(
                node_name=observed.node_name, node_uid=observed.node_uid
            )
        return task, observed

    def allocate(self):
        content = self.initialize()
        value = self.ledger.read(self.plan)
        if value["state"] == "rejected":
            raise AppError(
                422, "The original Lambda allocation was rejected", "lambda_allocation_rejected"
            )
        if value["state"] == "pending" and content is None:
            content, _ = self.io.cloud_init(self.plan.intent_id)
        try:
            if value["state"] in {"pending", "dispatching"}:
                with self.io.provider() as api:
                    if value["state"] == "pending":
                        self.ledger.record_preflight(
                            self.plan,
                            api.check_offering(
                                self.plan,
                                self.policy.max_price_cents_per_hour,
                                image_version=self.policy.image_version,
                            ),
                        )
                    allocate_once(self.plan, content, self.ledger, api)
        except AllocationAmbiguous:
            pending = self.sync()
            if pending.get("expires_at") and datetime.fromisoformat(pending["expires_at"]) <= now():
                self.checkpoint["request"]["expiry"] = True
                self.failed(
                    "lease_expired",
                    "Original allocation remains uncertain after its lease deadline",
                )
            return "Allocating · provider confirmation pending"
        except ProviderRejected:
            raise AppError(
                422,
                "Lambda rejected this allocation; no second launch was submitted",
                "lambda_allocation_rejected",
            ) from None
        self.sync()
        self.checkpoint["step"] = "host_access"
        return "Waiting for trusted host access"

    def host_access(self):
        value = self.sync()
        with self.io.provider() as api:
            instance = api.instance(value["instance_ids"][0])
        if instance is None or instance.get("status") in {"terminated", "preempted"}:
            self.failed("provider_instance_lost", "Allocated provider instance is no longer active")
            return "Cleanup pending · provider instance unavailable"
        owned_instances(self.plan, [instance], value["instance_ids"])
        attestation = self.intent().get("bootstrap", {})
        if not attestation.get("host_key"):
            if (
                not attestation.get("expires_at")
                or datetime.fromisoformat(attestation["expires_at"]) <= now()
            ):
                self.failed(
                    "host_attestation_expired", "Host identity was not attested before its deadline"
                )
                return "Cleanup pending · trusted host access failed"
            return "Waiting for trusted host identity"
        if (
            instance.get("status") != "active"
            or not instance.get("ip")
            or not instance.get("private_ip")
        ):
            return "Waiting for provider boot and network addresses"
        target = {
            "provider_instance_id": instance["id"],
            "node_name": "gpu-" + instance["id"],
            "public_ip": instance["ip"],
            "private_ip": instance["private_ip"],
            "trusted_host_key": attestation["host_key"],
        }
        _, task = self.task(
            "prepare", target=target
        )  # Freeze attested IP/key before first host dispatch.
        self.checkpoint["step"] = "prepare"
        self.state["members"][0]["phase"] = "configuring"
        return "Preparing host · " + task.node_name

    def advance_host(self, action):
        _, observed = self.host_step(action)
        if observed.status in {"queued", "running"}:
            return observed.stage, False
        if observed.status == "failed":
            self.failed(
                {
                    "prepare": "host_prepare_failed",
                    "join": "kubernetes_join_failed",
                    "inspect": "gpu_readiness_failed",
                }[action],
                "Trusted host execution failed; cleanup is required",
            )
            return "Cleanup pending · " + action + " failed", False
        if action == "prepare":
            if not observed.host_ready or not observed.public_wireguard_key:
                raise ProviderIdentityConflict("Preparation lacks host/public peer evidence")
            self.checkpoint["step"] = "join"
            self.state["phase"] = "joining"
            return "Configuring WireGuard and joining Kubernetes", False
        if action == "join":
            if not (
                observed.node_uid
                and observed.host_ready
                and observed.wireguard_ready
                and observed.kubernetes_ready
                and observed.cilium_ready
            ):
                raise ProviderIdentityConflict("Joined host lacks verified node/network evidence")
            self.state["members"][0]["phase"] = "joining"
            self.checkpoint["step"] = "inspect"
            return "Checking networking and operator-managed GPU software", False
        if not observed.gpu_runtime_ready:
            raise ProviderIdentityConflict("Inspection did not establish GPU readiness")
        self.state["members"][0].update(phase="gpu_ready", error=None)
        self.state["phase"] = "ready"
        return "Ready · verified host, networking and GPU capacity", True

    def finish_cleanup(self):
        self.sync()
        self.state["phase"] = (
            "expired"
            if self.checkpoint["request"].get("expiry")
            else "failed"
            if self.checkpoint.get("creation_failed")
            else "deleted"
        )
        return "Cleanup confirmed · " + self.state["phase"], True

    def cleanup(self):
        value = self.sync()
        self.state["phase"] = "draining"
        if value["state"] == "pending":
            self.ledger.abandon_pending(self.plan)
            return self.finish_cleanup()
        if value["state"] in {"rejected", "terminated"}:
            return self.finish_cleanup()
        if value["state"] == "dispatching":
            try:
                with self.io.provider() as api:
                    allocate_once(self.plan, None, self.ledger, api)
            except AllocationAmbiguous:
                return "Cleanup pending · original allocation remains uncertain", False
            self.sync()
            return "Cleanup pending · reconciled original allocation", False
        authorization = value.get("termination_authorization")
        if authorization is None:
            if value["host_tasks"] == {}:
                self.ledger.authorize_unbootstrapped(self.plan)
                return "Terminating allocation before host bootstrap", False
            active = value["host_tasks"][value["active_host_task_id"]]
            task = HostTask.model_validate(active["task"])
            observed = active.get("observation")
            if not observed or observed["status"] not in {"completed", "failed"}:
                store = DurableHostTasks(self.plan, self.io.host)
                # Frozen task deadlines precede lease expiry. Reposting an expired,
                # never-accepted task can only retain its terminal failure.
                if task.deadline <= now():
                    store.launch(task)
                else:
                    store.observe(task.id)
                return "Cleanup pending · waiting for original host task to stop", False
            known_uid = observed.get("node_uid") or task.node_uid
            action = (
                "remove"
                if known_uid and task.action in {"join", "inspect", "remove"}
                else "cleanup-check"
            )
            completed_task, completed = self.host_step(action)
            if completed.status == "failed":
                raise AppError(
                    409,
                    "Host removal/safety check failed; provider remains allocated",
                    "host_cleanup_failed",
                )
            if completed.status != "completed":
                return (
                    "Draining and removing worker"
                    if action == "remove"
                    else "Checking quarantined bootstrap safety",
                    False,
                )
            self.ledger.authorize_termination(self.plan, completed_task, completed)
            return "Host removal/safety confirmed · terminating provider allocation", False
        if value["state"] == "allocated" or value["state"] == "terminating":
            with self.io.provider() as api:
                fence = terminate_once(self.plan, self.ledger, api)
            return (
                "Provider termination confirmed"
                if fence
                else "Waiting for provider termination confirmation"
            ), False
        if value["state"] != "fenced":
            raise ProviderIdentityConflict("Unexpected real cleanup state")
        route = authorization["route"]
        if route == "unbootstrapped":
            self.ledger.unbootstrapped_cleanup_completed(self.plan)
        elif route == "drained":
            saved = value["host_tasks"][authorization["task_id"]]
            self.ledger.cleanup_completed(
                self.plan,
                HostTask.model_validate(saved["task"]),
                HostTaskObservation.model_validate(saved["observation"]),
            )
        else:
            task, observed = self.host_step("revoke-bootstrap")
            if observed.status == "failed":
                raise AppError(
                    409,
                    "Fenced bootstrap revocation failed; address remains reserved",
                    "host_cleanup_failed",
                )
            if observed.status != "completed":
                return "Revoking fenced bootstrap peers and node identity", False
            self.ledger.cleanup_completed(self.plan, task, observed)
        return self.finish_cleanup()

    def step(self):
        deadline = self.checkpoint.setdefault(
            "workflow_deadline",
            (
                now()
                + timedelta(
                    minutes=self.spec.ttl_minutes + 30
                    if self.op["type"] == "create_worker_group"
                    else 30
                )
            ).isoformat(),
        )
        if datetime.fromisoformat(deadline) <= now():
            raise AppError(
                408,
                "Real workflow deadline reached; retain unresolved cleanup obligations",
                "lifecycle_deadline",
            )
        stage = self.checkpoint.get(
            "step",
            "allocate"
            if self.op["type"] == "create_worker_group"
            else "renew"
            if self.op["type"] == "renew_worker_group"
            else "cleanup",
        )
        if stage != "allocate":
            intent = self.intent()
            if intent is None or not intent.get("lambda_launch"):
                if stage != "cleanup":
                    raise ProviderIdentityConflict("Real allocation plan is missing")
                if intent is not None:
                    with session() as db, db.begin():
                        row = db.get(ProviderIntent, intent["id"], with_for_update=True)
                        if row.state != "pending" or row.payload.get("lambda_launch"):
                            raise ProviderIdentityConflict(
                                "Unattempted intent changed during cleanup"
                            )
                        row.state = "rejected"
                        row.payload = {**row.payload, "unattempted_cleanup": True}
                self.state["phase"] = "deleted"
                return (
                    self.payload,
                    self.checkpoint,
                    True,
                    False,
                    "Deleted · no provider launch dispatched",
                )
            self.plan = LaunchPlan.model_validate(intent["lambda_launch"]["plan"])
            value = self.sync()
            if (
                stage != "cleanup"
                and value.get("expires_at")
                and datetime.fromisoformat(value["expires_at"]) <= now()
            ):
                self.checkpoint["request"]["expiry"] = True
                self.failed("lease_expired", "Worker lease expired; cleanup is required")
                stage = "cleanup"
        if stage == "allocate":
            label, done = self.allocate(), False
        elif stage == "host_access":
            label, done = self.host_access(), False
        elif stage in {"prepare", "join", "inspect"}:
            label, done = self.advance_host(stage)
        elif stage == "renew":
            self.state["expires_at"] = self.ledger.renew(
                self.plan,
                self.op["id"],
                self.checkpoint["request"]["additional_minutes"],
                self.policy.max_total_lease_minutes,
            )
            label, done = "Lease renewed · bounded and durable", True
        elif stage == "cleanup":
            label, done = self.cleanup()
        else:
            raise AppError(409, "Unknown real worker lifecycle stage")
        return (
            self.payload,
            self.checkpoint,
            done,
            bool(self.checkpoint.get("creation_failed")),
            label,
        )


def step_real_group(op, payload, checkpoint, binding, *, io=None):
    workflow = RealWorkerWorkflow(op, payload, checkpoint, binding, io)
    try:
        return workflow.step()
    except TransientProviderError:
        # Normal bounded observation waits do not exhaust C's five exception retries.
        # The persisted workflow deadline still terminates unavailable dependencies.
        return (
            workflow.payload,
            workflow.checkpoint,
            False,
            bool(workflow.checkpoint.get("creation_failed")),
            "Real lifecycle observation unavailable · retrying original intent",
        )


def recover_real_group(db, payload):
    """Recover committed provider/host side effects after a lost C checkpoint write."""
    status = payload["group"]["status"]
    members = {m["provider_instance_id"]: m for m in status["members"]}
    cleanup = False
    for intent in db.scalars(
        select(ProviderIntent).where(ProviderIntent.group_id == payload["id"])
    ):
        if intent.payload.get("mode") != "real":
            continue
        value = intent.payload.get("lambda_launch", {})
        if value.get("dispatched_at"):
            status.update(allocated_at=value["dispatched_at"], expires_at=value["expires_at"])
        cleanup = cleanup or value.get("state") in {
            "dispatching",
            "allocated",
            "terminating",
            "fenced",
        }
        active = value.get("host_tasks", {}).get(value.get("active_host_task_id"), {})
        observed = active.get("observation") or {}
        task = active.get("task") or {}
        for identity in value.get("instance_ids", []):
            member = members.setdefault(
                identity,
                {
                    "provider_instance_id": identity,
                    "phase": "allocated",
                    "node_name": None,
                    "node_uid": None,
                    "error": None,
                },
            )
            uid = observed.get("node_uid") or task.get("node_uid")
            if uid:
                member.update(node_name=task["node_name"], node_uid=uid)
            if value.get("state") == "terminated":
                member.update(phase="terminated", error=None)
    status["members"] = list(members.values())
    return cleanup or any(m["phase"] != "terminated" for m in status["members"])
