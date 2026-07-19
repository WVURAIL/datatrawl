"""
datatrawl.pipeline -- the storage-safe streaming engine.

This is the fixed part of the tool: a storage-safe streaming loop. For every
Unit in a selection it:

    fetch (downloader thread(s) stage files onto scratch)
    read  (Reader -> iterable of arrays)
    analyze (Analyzer.consume_file accumulates)
    delete the staged file immediately
    ask the Analyzer to checkpoint its product every N successfully consumed files

Scratch usage is bounded by a semaphore: at most `max_staged_files` files are on
disk at once, enforced across all downloader threads (a slot is freed only after
the consumer deletes the file). The default (`max_staged_files=1`,
`download_workers=1`) holds exactly one file at a time, in source order. Raising
either setting relaxes the source-order contract. Multiple staging slots allow
download/analyze overlap, and concurrent fetches require multiple workers and
multiple slots. An analyzer that needs source order must use the defaults.

Restartable: on restart the analyzer re-loads its product, reports which units it
already holds, and the engine processes only the rest.

The engine knows nothing about file layouts, power spectra, or N^2 visibilities
-- only Units, Readers, and Analyzers. That is what makes one engine serve every
telescope/source/reader/analyzer combination.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .interfaces import DataSource, Reader, Analyzer, RunContext, Unit


def _rm(path: Optional[str]) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _stage_name(unit: Unit) -> str:
    """Scratch filename derived from the stable key, so two units with the same
    basename (the same file under different paths, or a name reused across a
    selection) never collide on disk and corrupt each other's run."""
    h = hashlib.sha256(unit.key.encode("utf-8")).hexdigest()[:16]
    return f"{h}_{os.path.basename(unit.name) or 'file'}"


@dataclass
class RunResult:
    out_path: str
    n_total: int
    n_done: int
    n_new: int
    n_failed: int
    n_quarantined: int = 0


class _ReaderIterationError(RuntimeError):
    """A reader failed while yielding arrays from a staged file."""


def _reader_arrays(reader: Reader, path: str, ctx: RunContext):
    """Yield reader arrays while preserving the reader/analyzer error boundary."""
    try:
        yield from reader.iter_arrays(path, ctx)
    except Exception as exc:                              # noqa: BLE001
        raise _ReaderIterationError(f"{type(exc).__name__}: {exc}") from exc


def _quarantine_key(unit: Unit) -> str:
    """Stable logical identity used by the quarantine ledger.

    Unit.key is the generic default. A source may provide `quarantine_key` in
    metadata when the physical fetch URI can change while the logical file stays
    the same.
    """
    meta = unit.meta or {}
    value = meta.get("quarantine_key")
    return str(unit.key if value is None else value)


def _load_quarantine(path: Optional[str]) -> tuple[set[str], set[str]]:
    """Return (stable keys, legacy names) recorded as bad/unreadable.

    Current records use `quarantine_key` (or the historical `key`). Name-only
    records remain supported so existing ledgers continue to work.
    """
    keys: set[str] = set()
    legacy_names: set[str] = set()
    if not path or not os.path.exists(path):
        return keys, legacy_names
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:                                # noqa: BLE001
                continue
            key = rec.get("quarantine_key", rec.get("key"))
            if key is not None:
                keys.add(str(key))
            elif rec.get("name"):
                legacy_names.add(str(rec["name"]))
    return keys, legacy_names


def _append_quarantine(path: Optional[str], unit: Unit, reason: str) -> None:
    """Append a durable, reviewable record that this file was excluded."""
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rec = {
        "quarantine_key": _quarantine_key(unit),
        "key": unit.key,
        "name": unit.name,
        "reason": reason,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def run(
    *,
    source: DataSource,
    reader: Reader,
    analyzer: Analyzer,
    units: Iterable[Unit],
    out_path: str,
    tmp_dir: str,
    ctx: RunContext,
    checkpoint_every: int = 50,
    download_workers: int = 1,
    max_staged_files: int = 1,
    max_files: Optional[int] = None,
    max_frames_per_file: Optional[int] = None,
    quarantine_path: Optional[str] = None,
    verbose: bool = True,
) -> RunResult:
    units = list(units)
    n_total = len(units)
    if max_files:
        units = units[:max_files]

    # Correctness guard: an analyzer that depends on consume order (a CFAR
    # baseline, any running/trailing statistic) declares requires_in_order. The
    # default 1 worker / 1 slot delivers files in source order; raising either
    # setting relaxes that public contract and could silently change an
    # order-dependent product. Refuse the combination rather than produce a wrong
    # result. (A commutative analyzer leaves the flag False.)
    if getattr(analyzer, "requires_in_order", False) and (
            download_workers > 1 or max_staged_files > 1):
        name = getattr(getattr(analyzer, "info", None), "name", type(analyzer).__name__)
        raise SystemExit(
            f"analyzer {name!r} requires in-order file delivery, which is "
            f"incompatible with --download-workers {download_workers} / "
            f"--max-staged-files {max_staged_files} (these settings relax the "
            f"source-order contract). Rerun with --download-workers 1 and "
            f"--max-staged-files 1 (the defaults).")

    # Make engine-level run parameters visible to the analyzer BEFORE resume, so it
    # can stamp them into its product and refuse an incompatible resume (e.g. a
    # capped smoke-test product must not be silently "completed" by a full run).
    # RunContext accepts any Mapping, including immutable mappings supplied by
    # library callers. Copy at the engine boundary before adding run invariants.
    ctx.options = dict(ctx.options or {})
    ctx.options["max_frames_per_file"] = max_frames_per_file

    # Resume: let the analyzer re-load (and validate) its own product.
    resumed = analyzer.resume(out_path, ctx)
    done_keys = set(analyzer.processed_keys()) if resumed else set()
    if verbose and resumed:
        print(f"resume: {len(done_keys)} unit(s) already in {out_path}")

    # Quarantine: current records use a stable source-defined identity. Legacy
    # name-only ledgers remain readable but can be less precise.
    quarantined_keys, legacy_quarantined_names = _load_quarantine(quarantine_path)
    n_quarantined = len(quarantined_keys) + len(legacy_quarantined_names)
    if verbose and n_quarantined:
        print(f"quarantine: {n_quarantined} file(s) excluded as bad "
              f"(recorded in {quarantine_path})")

    todo = [u for u in units if u.key not in done_keys
            and _quarantine_key(u) not in quarantined_keys
            and u.name not in legacy_quarantined_names]
    if verbose:
        print(f"{n_total} unit(s) total, {len(done_keys)} done, "
              f"{n_quarantined} quarantined, {len(todo)} to process "
              f"-> {out_path}")
    if not todo:
        print("nothing to do -- selection already complete in this product")
        return RunResult(out_path, n_total, len(done_keys), 0, 0, 0)

    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # producer/consumer: downloader thread(s) stage files; the MAIN thread does
    # all reading, accumulation, and checkpointing -> single-writer, no locks.
    #
    # Storage safety: a bounded semaphore caps files-on-scratch at max_staged_files.
    # A downloader acquires a slot BEFORE staging a file; the slot is released by
    # the consumer only AFTER it deletes that file. So disk never exceeds the bound
    # regardless of how many download threads run. Default 1/1 = exactly one file
    # at a time, delivered in source order.
    n_workers = max(1, download_workers)
    n_slots = max(1, max_staged_files)
    stage_slots = threading.BoundedSemaphore(n_slots)
    work_q: "queue.Queue[Unit]" = queue.Queue()
    for u in todo:
        work_q.put(u)
    ready_q: "queue.Queue[tuple]" = queue.Queue(maxsize=n_slots + 1)
    stop = threading.Event()

    def _queue_ready(item: tuple) -> bool:
        """Queue a fetched item, but let cancellation wake blocked producers."""
        while not stop.is_set():
            try:
                ready_q.put(item, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def _discard_ready() -> None:
        """Delete staged files that will not be consumed after an early abort."""
        while True:
            try:
                _unit, dest, _ok, _err = ready_q.get_nowait()
            except queue.Empty:
                return
            _rm(dest)
            stage_slots.release()

    def _downloader() -> None:
        while not stop.is_set():
            try:
                unit = work_q.get_nowait()
            except queue.Empty:
                return
            stage_slots.acquire()                # reserve a disk slot before staging
            if stop.is_set():
                stage_slots.release()
                return
            dest = os.path.join(tmp_dir, _stage_name(unit))
            try:
                ok, err = source.fetch(unit, dest)
            except Exception as exc:                       # noqa: BLE001
                ok, err = False, f"{type(exc).__name__}: {exc}"
            if not _queue_ready((unit, dest, ok, err)):
                _rm(dest)
                stage_slots.release()
                return

    workers = [threading.Thread(target=_downloader, daemon=True)
               for _ in range(n_workers)]
    for w in workers:
        w.start()
    if verbose:
        print(f"streaming with {len(workers)} download worker(s), "
              f"<= {n_slots} file(s) on scratch", flush=True)

    started = False
    t0, got, fail, quar = time.time(), 0, 0, 0
    try:
        for _ in range(len(todo)):           # exactly one ready item per todo unit
            unit, dest, ok, err = ready_q.get()
            try:
                if not ok:
                    # fetch failure == transient (network/cert) -> retry on re-run
                    fail += 1
                    print(f"  FAIL fetch {unit.name}: {err}", file=sys.stderr)
                    continue
                try:
                    meta = dict(reader.probe(dest))
                except Exception as exc:                    # noqa: BLE001
                    # bytes arrived but won't read (bad header/corrupt): the file is
                    # deterministically bad. Quarantine (record + skip for good)
                    # rather than failing the run forever.
                    reason = f"probe/read: {type(exc).__name__}: {exc}"
                    if quarantine_path:
                        quar += 1
                        quarantined_keys.add(_quarantine_key(unit))
                        _append_quarantine(quarantine_path, unit, reason)
                        print(f"  QUARANTINE {unit.name}: {reason}", file=sys.stderr)
                    else:
                        fail += 1
                        print(f"  FAIL read {unit.name}: {reason}", file=sys.stderr)
                    continue
                meta.update(unit.meta)
                meta["unit_key"] = unit.key
                meta["unit_name"] = unit.name
                if not started:
                    analyzer.begin(ctx, meta)     # may raise SystemExit on a bad resume
                    started = True
                try:
                    arrays = _reader_arrays(reader, dest, ctx)
                    if max_frames_per_file:
                        arrays = itertools.islice(arrays, max_frames_per_file)
                    analyzer.consume_file(arrays, meta)
                except _ReaderIterationError as exc:
                    reason = f"read: {exc}"
                    if quarantine_path:
                        quarantined_keys.add(_quarantine_key(unit))
                        _append_quarantine(quarantine_path, unit, reason)
                        print(f"  QUARANTINE {unit.name}: {reason}", file=sys.stderr)
                        disposition = (
                            "The file was quarantined; rerun the same command to "
                            "resume without it."
                        )
                    else:
                        disposition = (
                            "Quarantine is disabled; fix or remove the file before "
                            "rerunning."
                        )
                    raise RuntimeError(
                        f"reader failed while streaming {unit.name}: {exc}. "
                        "The current analyzer state was not checkpointed. "
                        f"{disposition}"
                    ) from exc
                except Exception as exc:                    # noqa: BLE001
                    analyzer_name = getattr(
                        getattr(analyzer, "info", None),
                        "name",
                        type(analyzer).__name__,
                    )
                    raise RuntimeError(
                        f"analyzer {analyzer_name!r} failed on {unit.name}: "
                        f"{type(exc).__name__}: {exc}. The file was not "
                        "quarantined; analyzer exceptions are run-level errors."
                    ) from exc
                got += 1
                done_keys.add(unit.key)
                if verbose and got % 25 == 0:
                    s = analyzer.summary()
                    rate = (s.get("count", 0)) / max(time.time() - t0, 1e-9)
                    print(f"  [{len(done_keys)}/{n_total}] {got} new, {fail} failed, "
                          f"{quar} quarantined, {rate:.1f} unit-frames/s  {s}",
                          flush=True)
                if got % checkpoint_every == 0:
                    analyzer.save(out_path)
                    if verbose:
                        print(f"  ...checkpoint ({got} new, {len(done_keys)} total)",
                              flush=True)
            finally:
                _rm(dest)               # delete the staged file...
                stage_slots.release()   # ...then free its slot for the next download
    finally:
        stop.set()
        _discard_ready()
        for w in workers:
            w.join(timeout=2.0)
        # A fetch may have completed while the workers were being joined.
        _discard_ready()

    if started:
        analyzer.save(out_path)
    quar_note = f", {quar} quarantined" if quar else ""
    print(f"done: {len(done_keys)}/{n_total} units, {got} new this run, "
          f"{fail} failed{quar_note} | {analyzer.summary()}")
    print(f"product: {out_path}")
    return RunResult(out_path, n_total, len(done_keys), got, fail, quar)
