"""AIPerf 0.13.0 extensions: native scheduling/HTTP remain the execution engine."""

import asyncio
import contextvars
import json
import os
import time
from functools import lru_cache
from pathlib import Path

from aiperf.common.enums import ConversationContextMode
from aiperf.common.hooks import on_stop
from aiperf.common.models import Conversation, Turn
from aiperf.common.models.error_models import ErrorDetails
from aiperf.common.models.record_models import RequestRecord
from aiperf.credit.structs import TurnToSend
from aiperf.dataset.loader.raw_payload import RawPayloadDatasetLoader
from aiperf.timing.manager import TimingManager
from aiperf.timing.strategies.fixed_schedule import FixedScheduleStrategy
from aiperf.transports.aiohttp_client import AioHttpClient
from aiperf.transports.aiohttp_transport import AioHttpTransport

from workload_runner.adapters.journal import journal
from workload_runner.artifacts import atomic_json


def run_directory():
    value = os.environ.get("PLATFORM_RUN_DIR")
    return Path(value) if value else None


def event(value):
    directory = run_directory()
    if directory:
        journal(str(directory), os.getpid()).emit(value)


def runtime_directory():
    return Path(os.environ["PLATFORM_LOCAL_DIR"])


@lru_cache(maxsize=4)
def engine_context(directory):
    return json.loads((directory / "engine-context.json").read_text())


@lru_cache(maxsize=4)
def engine_clock(directory):
    return json.loads((directory / "engine-clock.json").read_text())


async def close_journal():
    if directory := run_directory():
        await asyncio.to_thread(journal(str(directory), os.getpid()).close)


class PlatformTimingManager(TimingManager):
    @on_stop
    async def _timing_manager_stop(self):
        try:
            await super()._timing_manager_stop()
        finally:
            await close_journal()


class PlatformLoader(RawPayloadDatasetLoader):
    def load_dataset(self):
        if run_directory() is None:
            return super().load_dataset()
        with Path(self.filename).open() as stream:
            return {item["session_id"]: [item] for line in stream if (item := json.loads(line))}

    def convert_to_conversations(self, data):
        if run_directory() is None:
            return super().convert_to_conversations(data)
        conversations = []
        for session_id, items in data.items():
            row = items[0]
            turns = []
            for index, item in enumerate(row["turns"]):
                payload = item["payload"]
                kwargs = dict(
                    timestamp=row["timestamp_ms"] if index == 0 else None,
                    delay=None if index == 0 else item["delay_ms"],
                    extra_headers=item["headers"],
                    model=payload["model"],
                )
                if row["context_mode"] == "recorded":
                    turn = Turn(role="user", raw_payload=payload, **kwargs)
                else:
                    turn = Turn(
                        role="user",
                        raw_messages=payload["messages"],
                        max_tokens=payload["max_tokens"],
                        raw_tools=payload.get("tools"),
                        extra_body={
                            k: v
                            for k, v in payload.items()
                            if k not in {"model", "messages", "tools", "max_tokens"}
                        },
                        **kwargs,
                    )
                turns.append(turn)
            conversations.append(
                Conversation(
                    session_id=session_id,
                    turns=turns,
                    context_mode=(
                        ConversationContextMode.MESSAGE_ARRAY_WITH_RESPONSES
                        if row["context_mode"] == "recorded"
                        else ConversationContextMode.DELTAS_WITH_RESPONSES
                    ),
                )
            )
        return conversations


class PlatformSchedule(FixedScheduleStrategy):
    def _retire_continuations(self, credit, reason):
        """Drain native credits without making further HTTP calls.

        0.13.0 retains a session slot until its authored final credit returns.
        Retiring the remaining credits avoids leaking that slot on early abort.
        These diagnostic records never become offered client requests.
        """
        atomic_json(
            runtime_directory() / "session-state" / (credit.conversation_id + ".json"),
            {"reason": reason},
        )
        if not credit.is_final_turn:
            meta = self._conversation_source.get_next_turn_metadata(credit)
            turn = TurnToSend.from_previous_credit(credit, meta)
            self._scheduler.execute_async(self._credit_issuer.issue_credit(turn))

    async def execute_phase(self):
        if directory := run_directory():
            journal(str(directory), os.getpid())
            origin = self._lifecycle.started_at_perf_ns
            atomic_json(
                directory / "engine-clock.json",
                {"perf_ns": origin, "epoch_ns": time.time_ns() - (time.perf_counter_ns() - origin)},
            )
            atomic_json(runtime_directory() / "engine-clock.json", engine_clock(directory))
        await super().execute_phase()

    async def handle_credit_return(self, credit, *, error=None):
        if run_directory() and error:
            event(
                {
                    "kind": "session_abort",
                    "conversation_id": credit.conversation_id,
                    "error": str(error),
                }
            )
            self._retire_continuations(credit, "aborted")
            return
        if (directory := run_directory()) and not credit.is_final_turn:
            context = engine_context(directory)
            meta = self._conversation_source.get_next_turn_metadata(credit)
            clock = engine_clock(runtime_directory())
            due = (
                (time.perf_counter_ns() - clock["perf_ns"]) / 1e6 + (meta.delay_ms or 0)
                if meta.timestamp_ms is None
                else meta.timestamp_ms
            )
            if (directory / "cancel-requested").exists() or due >= context["deadline_ms"]:
                event(
                    {
                        "kind": "cutoff",
                        "conversation_id": credit.conversation_id,
                        "turn_index": credit.turn_index + 1,
                    }
                )
                self._retire_continuations(credit, "cutoff")
                return
        await super().handle_credit_return(credit, error=error)


class PlatformHTTP(AioHttpTransport):
    def __init__(self, **kwargs):
        self._selected = contextvars.ContextVar("platform_http_client", default=None)
        self._fallback = None
        self._clients = {}
        self._limits = {}
        super().__init__(**kwargs)
        directory = run_directory()
        self._context = engine_context(directory) if directory else None
        if directory:
            journal(str(directory), os.getpid())

    @property
    def aiohttp_client(self):
        return self._selected.get() or self._fallback

    @aiohttp_client.setter
    def aiohttp_client(self, value):
        self._fallback = value

    @on_stop
    async def _close_platform_clients(self):
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        await close_journal()

    async def send_request(self, request_info, payload, *, first_token_callback=None):
        if self._context is None:
            return await super().send_request(
                request_info, payload, first_token_callback=first_token_callback
            )
        session_id = request_info.conversation_id
        index = request_info.turn_index
        plan = self._context["sessions"][session_id]
        ref = plan["customer_ref"]
        customer = self._context["customers"][ref]
        if (runtime_directory() / "session-state" / (session_id + ".json")).exists():
            now = time.perf_counter_ns()
            event({"kind": "suppressed", "conversation_id": session_id, "turn_index": index})
            return RequestRecord(
                start_perf_ns=now,
                end_perf_ns=now,
                error=ErrorDetails(type="PlatformSuppressed", message="Session already terminated"),
            )
        limiter = self._limits.setdefault(ref, asyncio.Semaphore(customer["max_inflight"]))
        if ref not in self._clients:
            self._clients[ref] = AioHttpClient(timeout=customer["timeout_ms"] / 1000)
        if isinstance(payload, bytes):
            payload = json.loads(payload)
        request_info = request_info.model_copy(deep=True)
        # Large raw-payload mmap replay doesn't retain Turn.extra_headers in 0.13.0.
        # Apply the frozen per-turn headers at the native transport boundary too.
        request_info.endpoint_headers.update(plan["headers"][index])
        request_info.model_endpoint.endpoint.streaming = bool(payload.get("stream"))
        if variable := customer.get("credential_env"):
            value = os.environ.get(variable)
            if not value:
                raise ValueError("missing endpoint credential environment variable")
            request_info.endpoint_headers["Authorization"] = "Bearer " + value
        async with limiter:
            directory = run_directory()
            clock = engine_clock(runtime_directory())
            now = time.perf_counter_ns()
            offset_ms = (now - clock["perf_ns"]) / 1e6
            if (
                (directory / "cancel-requested").exists()
                or offset_ms >= self._context["deadline_ms"]
                or index == 0
                and offset_ms >= self._context["admission_end_ms"]
            ):
                event({"kind": "cutoff", "conversation_id": session_id, "turn_index": index})
                return RequestRecord(
                    start_perf_ns=now,
                    end_perf_ns=now,
                    error=ErrorDetails(type="PlatformCutoff", message="Execution drain deadline"),
                )
            # Native aiohttp cancellation enforces the remaining drain budget, even
            # when a customer's normal timeout is much longer than the drain period.
            deadline_ns = clock["perf_ns"] + int(self._context["deadline_ms"] * 1e6)
            remaining_ns = max(1, deadline_ns - now)
            request_info.cancel_after_ns = min(
                request_info.cancel_after_ns or remaining_ns, remaining_ns
            )
            event(
                {
                    "kind": "dispatch",
                    "conversation_id": session_id,
                    "turn_index": index,
                    "perf_ns": now,
                }
            )
            token = self._selected.set(self._clients[ref])
            try:
                record = await super().send_request(
                    request_info, payload, first_token_callback=first_token_callback
                )
            finally:
                self._selected.reset(token)
            event(
                {
                    "kind": "record",
                    "conversation_id": session_id,
                    "turn_index": index,
                    "payload": payload,
                    "record": record.model_dump(mode="json"),
                }
            )
            from workload_runner.adapters.engine import response_parts

            _, _, _, finish, _ = response_parts(record.model_dump(mode="json"))
            if not record.error and (not finish or finish == "length"):
                record.error = ErrorDetails(type="PlatformIncomplete", message="Incomplete output")
            return record
