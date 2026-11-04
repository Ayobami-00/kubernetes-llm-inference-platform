"""Bounded background persistence; HTTP coroutines never wait for shared-storage locks."""

import atexit
import fcntl
import json
import os
import threading
import time
from collections import deque
from functools import lru_cache
from multiprocessing.util import Finalize
from pathlib import Path

from workload_runner.artifacts import atomic_json

MAX_PENDING_BYTES = 32 * 1024**2
MAX_PENDING_EVENTS = 4096
FLUSH_INTERVAL_S = 0.025


def append_batch(path, payload):
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


class BoundedJournal:
    def __init__(
        self,
        path,
        receipts,
        *,
        max_bytes=MAX_PENDING_BYTES,
        max_events=MAX_PENDING_EVENTS,
        flush_interval=FLUSH_INTERVAL_S,
        sink=append_batch,
    ):
        self.path = Path(path)
        self.receipts = Path(receipts)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.identity = str(os.getpid())
        self.max_bytes, self.max_events = max_bytes, max_events
        self.flush_interval, self.sink = flush_interval, sink
        self.pending = deque()
        self.pending_bytes = 0
        self.accepted = self.written = 0
        self.closed = False
        self.failure = None
        self.condition = threading.Condition()
        atomic_json(
            self.receipts / (self.identity + ".started.json"),
            {
                "max_pending_bytes": max_bytes,
                "max_pending_events": max_events,
                "flush_interval_s": flush_interval,
            },
        )
        self.thread = threading.Thread(target=self._write, name="transport-journal", daemon=True)
        self.thread.start()
        atexit.register(self._at_exit)
        # multiprocessing's Linux exit path calls os._exit after its own
        # finalizers, bypassing ordinary atexit callbacks. Strategies are not
        # lifecycle components, so their @on_stop methods are not discovered.
        self.finalizer = Finalize(None, self._at_exit, exitpriority=10)

    def emit(self, value):
        payload = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
        with self.condition:
            if self.closed or self.failure:
                raise RuntimeError("Transport journal is unavailable")
            if (
                self.accepted - self.written >= self.max_events
                or self.pending_bytes + len(payload) > self.max_bytes
            ):
                self.failure = RuntimeError("Transport journal capacity exceeded")
                self.condition.notify_all()
                raise RuntimeError("Transport journal capacity exceeded")
            self.pending.append(payload)
            self.pending_bytes += len(payload)
            self.accepted += 1
            self.condition.notify_all()

    def _write(self):
        try:
            while True:
                with self.condition:
                    while not self.pending and not self.closed and not self.failure:
                        self.condition.wait()
                    if not self.pending:
                        break
                    until = time.monotonic() + self.flush_interval
                    while not self.closed and not self.failure and len(self.pending) < 128:
                        remaining = until - time.monotonic()
                        if remaining <= 0:
                            break
                        self.condition.wait(timeout=remaining)
                    batch = []
                    size = 0
                    while self.pending and (not batch or size + len(self.pending[0]) <= 256 * 1024):
                        item = self.pending.popleft()
                        batch.append(item)
                        size += len(item)
                # Only this worker blocks on NFS. In-flight bytes still count
                # against the producer's memory bound until the batch is durable.
                self.sink(self.path, b"".join(batch))
                with self.condition:
                    self.pending_bytes -= size
                    self.written += len(batch)
        except Exception as error:
            with self.condition:
                self.failure = error
        finally:
            with self.condition:
                healthy = self.closed and not self.failure and self.accepted == self.written
                receipt = {
                    "status": "drained" if healthy else "failed",
                    "accepted_events": self.accepted,
                    "written_events": self.written,
                    "error_type": type(self.failure).__name__ if self.failure else None,
                }
            atomic_json(self.receipts / (self.identity + ".finished.json"), receipt)

    def close(self, timeout=10):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.thread.join(timeout)
        if self.thread.is_alive() or self.failure:
            raise RuntimeError("Transport journal failed to drain")

    def _at_exit(self):
        try:
            self.close()
        except RuntimeError:
            # The parent also requires matching healthy process receipts. A lost
            # writer cannot become a valid result because a shutdown hook failed.
            pass


@lru_cache(maxsize=4)
def journal(directory, process_id):
    del process_id  # Cache identity prevents inheriting a parent's writer after fork.
    local = os.environ.get("PLATFORM_LOCAL_DIR")
    if not local:
        raise ValueError("AIPerf requires an attempt-local runtime directory")
    return BoundedJournal(
        Path(directory) / "aiperf/transport-events.jsonl", Path(local) / "journal-receipts"
    )


def journal_status(local):
    folder = Path(local) / "journal-receipts"
    started = sorted(folder.glob("*.started.json"))
    records = []
    for path in started:
        final = path.with_name(path.name.replace(".started.", ".finished."))
        value = json.loads(final.read_text()) if final.is_file() else {"status": "missing"}
        records.append({"process_id": path.name.split(".")[0], **value})
    healthy = bool(records) and all(
        r["status"] == "drained" and r.get("accepted_events") == r.get("written_events")
        for r in records
    )
    return {
        "status": "drained" if healthy else "failed",
        "journals": records,
        "max_pending_bytes_per_process": MAX_PENDING_BYTES,
        "max_pending_events_per_process": MAX_PENDING_EVENTS,
        "flush_interval_s": FLUSH_INTERVAL_S,
        "limitations": (
            "A hard Pod loss can lose pending local events; "
            "the attempt fails and is never restarted"
        ),
    }
