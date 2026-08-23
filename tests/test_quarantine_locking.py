"""Cross-process integrity for the source/reader quarantine ledger."""
from __future__ import annotations

import builtins
import errno
import json
import multiprocessing
import os

import pytest

from datatrawl import pipeline
from datatrawl.interfaces import Unit


_WAIT_SECONDS = 10


def _hold_ledger_lock(ledger, acquired, release):
    with pipeline._QuarantineLedgerLock(ledger):
        acquired.set()
        if not release.wait(_WAIT_SECONDS):
            raise TimeoutError("test did not release quarantine ledger lock")


def _append_after_signal(ledger, key, started, done):
    started.set()
    pipeline._append_quarantine(
        ledger, Unit(key=key, name=f"{key}.dat"), "bad header")
    done.set()


def _append_under_product_lock(out_path, ledger, key, ready, start):
    # Distinct product paths prove the ordinary output locks do not serialize
    # these scans. The shared quarantine sidecar owns only the ledger operation.
    with pipeline._OutputLock(out_path):
        ready.put(os.getpid())
        if not start.wait(_WAIT_SECONDS):
            raise TimeoutError("test did not start concurrent ledger appends")
        pipeline._append_quarantine(
            ledger, Unit(key=key, name=f"{key}.dat"), "bad header")


def test_append_waits_for_active_ledger_owner(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ledger = str(tmp_path / "quarantine.jsonl")
    acquired = ctx.Event()
    release = ctx.Event()
    started = ctx.Event()
    done = ctx.Event()
    holder = ctx.Process(
        target=_hold_ledger_lock, args=(ledger, acquired, release))
    writer = ctx.Process(
        target=_append_after_signal,
        args=(ledger, "shared-unit", started, done),
    )

    holder.start()
    assert acquired.wait(_WAIT_SECONDS)
    writer.start()
    try:
        assert started.wait(_WAIT_SECONDS)
        assert not done.wait(0.25), (
            "append completed while another process still owned the ledger")
    finally:
        release.set()

    assert done.wait(_WAIT_SECONDS)
    holder.join(_WAIT_SECONDS)
    writer.join(_WAIT_SECONDS)
    assert holder.exitcode == 0
    assert writer.exitcode == 0
    assert pipeline._load_quarantine(ledger) == {"shared-unit"}
    assert os.path.exists(pipeline._QuarantineLedgerLock(ledger).path)


def test_distinct_product_locks_deduplicate_concurrent_ledger_append(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ledger = str(tmp_path / "quarantine.jsonl")
    ready = ctx.Queue()
    start = ctx.Event()
    workers = [
        ctx.Process(
            target=_append_under_product_lock,
            args=(
                str(tmp_path / f"product-{index}.npz"),
                ledger,
                "same-logical-unit",
                ready,
                start,
            ),
        )
        for index in range(4)
    ]
    for worker in workers:
        worker.start()
    for _worker in workers:
        ready.get(timeout=_WAIT_SECONDS)
    start.set()
    for worker in workers:
        worker.join(_WAIT_SECONDS)
        assert worker.exitcode == 0

    with open(ledger, encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    assert len(records) == 1
    assert records[0]["quarantine_key"] == "same-logical-unit"
    assert pipeline._load_quarantine(ledger) == {"same-logical-unit"}


def test_unsupported_locking_is_a_specific_actionable_error(
        tmp_path, monkeypatch):
    ledger = str(tmp_path / "quarantine.jsonl")
    lock_path = pipeline._QuarantineLedgerLock(ledger).path
    real_open = builtins.open

    def unsupported(path, *args, **kwargs):
        if os.fspath(path) == lock_path:
            raise OSError(errno.EOPNOTSUPP, "operation not supported")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", unsupported)
    with pytest.raises(
            pipeline.QuarantineLedgerLockError,
            match=r"honors advisory file locks.*--quarantine.*--no-quarantine",
    ):
        pipeline._load_quarantine(ledger)
