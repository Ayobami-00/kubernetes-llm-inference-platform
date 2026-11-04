"""Management's durable handoff to the isolated host executor.

Persist immutable task inputs before transport. Terminal observations are local
history: they do not require new credentials or a live executor on repeated calls.
This module does not possess SSH keys or Kubernetes administrative authority.
"""

import copy

from platform_contracts import HostTask, HostTaskObservation
from platform_contracts.host_operations import validate_host_followup

from platform_management.db.models import ProviderIntent
from platform_management.db.session import session
from platform_management.providers.lambda_ledger import PostgresLaunchLedger
from platform_management.providers.lambda_mutations import ProviderIdentityConflict
from platform_management.services.common import digest


def task_digest(task):
    document = task.model_dump(mode="json")
    if document.get("provider_fence") is None:
        document.pop("provider_fence", None)
    return digest(document)


class DurableHostTasks:
    def __init__(self, plan, client_factory):
        self.plan, self.client_factory = plan, client_factory

    def read(self, identity):
        with session() as db:
            value = PostgresLaunchLedger.checked(
                db.get(ProviderIntent, self.plan.intent_id), self.plan
            )
            receipt = value.get("host_tasks", {}).get(identity)
            if receipt is None:
                raise ProviderIdentityConflict("Host task has not been frozen by this allocation")
            return copy.deepcopy(receipt)

    def freeze(self, task: HostTask):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, self.plan.intent_id, with_for_update=True)
            value = PostgresLaunchLedger.checked(row, self.plan)
            tasks = copy.deepcopy(value.get("host_tasks", {}))
            if task.id in tasks:
                if tasks[task.id]["task"] != task.model_dump(mode="json"):
                    raise ProviderIdentityConflict("Frozen host task inputs changed")
                return
            if value["instance_ids"] != [task.provider_instance_id]:
                raise ProviderIdentityConflict("Host task differs from its owned provider instance")
            reservation = value.get("wireguard_reservation", {})
            if reservation.get("address") != str(
                task.wireguard_ip
            ) or task.registry_revision < reservation.get("registry_revision", 1):
                raise ProviderIdentityConflict(
                    "Host task differs from its durable WireGuard reservation"
                )
            if task.action == "revoke-bootstrap":
                if value["state"] != "fenced" or task.provider_fence.model_dump(
                    mode="json"
                ) != value.get("provider_fence"):
                    raise ProviderIdentityConflict(
                        "Host revocation lacks the allocation's provider fence"
                    )
            elif value["state"] != "allocated" or value.get("termination_authorization"):
                raise ProviderIdentityConflict(
                    "Host configuration cannot resume after termination authorization"
                )
            active = value.get("active_host_task_id")
            if task.previous_task_id != active:
                raise ProviderIdentityConflict("New host work must follow the durable active task")
            if active:
                prior = tasks[active]
                previous = HostTask.model_validate(prior["task"])
                observed = prior.get("observation")
                if not observed:
                    raise ProviderIdentityConflict(
                        "Prior host task has not reached a terminal observation"
                    )
                try:
                    validate_host_followup(
                        task, previous, HostTaskObservation.model_validate(observed)
                    )
                except ValueError as error:
                    raise ProviderIdentityConflict(str(error)) from error
            elif task.action != "prepare":
                raise ProviderIdentityConflict("The first host task must prepare the host")
            tasks[task.id] = {
                "task": task.model_dump(mode="json"),
                "input_sha256": task_digest(task),
                "observation": None,
            }
            row.payload = {
                **row.payload,
                "lambda_launch": {**value, "host_tasks": tasks, "active_host_task_id": task.id},
            }

    def record(self, observation: HostTaskObservation):
        with session() as db, db.begin():
            row = db.get(ProviderIntent, self.plan.intent_id, with_for_update=True)
            value = PostgresLaunchLedger.checked(row, self.plan)
            tasks = copy.deepcopy(value.get("host_tasks", {}))
            receipt = tasks.get(observation.task_id)
            if not receipt or receipt["input_sha256"] != observation.input_sha256:
                raise ProviderIdentityConflict("Executor observation differs from its frozen task")
            task = HostTask.model_validate(receipt["task"])
            if (
                (observation.node_name and observation.node_name != task.node_name)
                or (
                    observation.node_uid and task.node_uid and observation.node_uid != task.node_uid
                )
                or observation.registry_revision < task.registry_revision
                or (
                    observation.status == "completed"
                    and task.node_uid is not None
                    and observation.node_uid != task.node_uid
                )
                or (observation.gpu_runtime_ready and task.action != "inspect")
                or (observation.bootstrap_fence_permitted and task.action != "cleanup-check")
                or (observation.bootstrap_cleanup_complete and task.action != "revoke-bootstrap")
                or (
                    receipt.get("job_uid")
                    and observation.job_uid
                    and receipt["job_uid"] != observation.job_uid
                )
            ):
                raise ProviderIdentityConflict(
                    "Executor changed the bound native execution or node identity"
                )
            previous = receipt.get("observation")
            if previous and previous["status"] in {"completed", "failed"}:
                return HostTaskObservation.model_validate(previous)
            receipt["observation"] = observation.model_dump(mode="json")
            receipt["job_uid"] = receipt.get("job_uid") or observation.job_uid
            row.payload = {**row.payload, "lambda_launch": {**value, "host_tasks": tasks}}
            return observation

    def _call(self, method, value):
        client = self.client_factory()
        try:
            return self.record(getattr(client, method)(value))
        finally:
            client.close()

    def launch(self, task: HostTask):
        self.freeze(task)
        retained = self.read(task.id).get("observation")
        if retained and retained["status"] in {"completed", "failed"}:
            return HostTaskObservation.model_validate(retained)
        return self._call("launch", task)

    def observe(self, identity):
        retained = self.read(identity).get("observation")
        if retained and retained["status"] in {"completed", "failed"}:
            return HostTaskObservation.model_validate(retained)
        return self._call("observe", identity)
