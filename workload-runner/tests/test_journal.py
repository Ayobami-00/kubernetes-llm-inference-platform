"""Slow storage cannot block admission, overflow memory bounds or silently lose records."""

import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from workload_runner.adapters.journal import BoundedJournal, append_batch, journal_status


def child_journal(directory, abrupt):
    from pathlib import Path

    root = Path(directory)
    journal = BoundedJournal(root / "events", root / "journal-receipts")
    journal.emit({"fixture": True})
    if abrupt:
        os._exit(2)
    # Deliberately no explicit close: exercise multiprocessing's exit path.


@pytest.mark.parametrize("method", ["spawn", "fork"])
@pytest.mark.parametrize("abrupt", [False, True])
def test_process_finalizers_flush_without_atexit_but_abrupt_loss_stays_invalid(
    tmp_path, method, abrupt
):
    if method not in multiprocessing.get_all_start_methods():
        pytest.skip("Start method unavailable on this host")
    process = multiprocessing.get_context(method).Process(
        target=child_journal, args=(str(tmp_path), abrupt)
    )
    process.start()
    process.join(timeout=15)
    assert not process.is_alive()
    assert process.exitcode == (2 if abrupt else 0)
    assert journal_status(tmp_path)["status"] == ("failed" if abrupt else "drained")


def test_slow_sink_does_not_block_producer_and_drains_every_event(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def slow(path, data):
        entered.set()
        assert release.wait(5)
        append_batch(path, data)

    journal = BoundedJournal(tmp_path / "events", tmp_path / "journal-receipts", sink=slow)
    try:
        journal.emit({"event": 0})
        assert entered.wait(2)
        # The sink remains blocked while all further admissions are recorded in
        # bounded memory. This would deadlock with synchronous request-path I/O.
        with ThreadPoolExecutor(max_workers=4) as producers:
            list(producers.map(lambda i: journal.emit({"event": i}), range(1, 100)))
        assert journal.accepted == 100 and journal.written == 0
    finally:
        release.set()
        journal.close()
    rows = [json.loads(line) for line in (tmp_path / "events").read_text().splitlines()]
    assert sorted(r["event"] for r in rows) == list(range(100))
    assert journal_status(tmp_path)["status"] == "drained"
    assert (tmp_path / "events").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bound", ["bytes", "events"])
def test_pending_and_inflight_limits_fail_closed(tmp_path, bound):
    entered, release = threading.Event(), threading.Event()

    def slow(path, data):
        entered.set()
        assert release.wait(5)
        append_batch(path, data)

    limits = {"max_bytes": 12} if bound == "bytes" else {"max_events": 1}
    journal = BoundedJournal(
        tmp_path / "events", tmp_path / "journal-receipts", sink=slow, **limits
    )
    journal.emit({"n": 1})
    assert entered.wait(2)
    try:
        with pytest.raises(RuntimeError, match="capacity"):
            journal.emit({"n": 2})
    finally:
        release.set()
        with pytest.raises(RuntimeError, match="drain"):
            journal.close()
    assert journal_status(tmp_path)["status"] == "failed"
    assert journal.accepted == journal.written == 1


def test_failed_storage_and_missing_final_receipt_can_never_pass(tmp_path):
    def broken(*_):
        raise OSError("synthetic disk failure")

    journal = BoundedJournal(tmp_path / "events", tmp_path / "journal-receipts", sink=broken)
    journal.emit({"n": 1})
    with pytest.raises(RuntimeError, match="drain"):
        journal.close()
    assert journal_status(tmp_path)["status"] == "failed"
    for path in (tmp_path / "journal-receipts").glob("*.finished.json"):
        path.unlink()
    assert journal_status(tmp_path)["status"] == "failed"
    assert journal_status(tmp_path)["journals"][0]["status"] == "missing"
    assert journal_status(tmp_path / "empty")["status"] == "failed"
