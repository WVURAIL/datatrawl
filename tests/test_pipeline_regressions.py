"""Focused regressions for pipeline worker, scratch, and product outcomes."""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import pytest

from datatrawl import pipeline
from datatrawl.interfaces import (
    Analyzer,
    DataSource,
    PluginInfo,
    READY,
    Reader,
    RunContext,
    STREAM_COMPLEX_BASEBAND,
    Unit,
    UnreadableUnitError,
)


class _ByteSource(DataSource):
    info = PluginInfo(
        name="bytes",
        kind="source",
        summary="test byte source",
        status=READY,
        instruments=("*",),
    )

    def __init__(self, *, terminal: Optional[BaseException] = None):
        self.terminal = terminal
        self.fetch_calls: list[str] = []

    def enumerate(self, ctx):
        return []

    def fetch(self, unit, dest):
        self.fetch_calls.append(unit.key)
        with open(dest, "wb") as fh:
            fh.write(b"staged")
        if self.terminal is not None:
            raise self.terminal
        return True, ""


class _BlockingSource(_ByteSource):
    """Keep one fetch active while another unit reaches the analyzer."""

    def __init__(self):
        super().__init__()
        self.blocking_fetch_started = threading.Event()
        self.release_fetch = threading.Event()
        self.blocking_fetch_returned = threading.Event()

    def fetch(self, unit, dest):
        self.fetch_calls.append(unit.key)
        if unit.key == "unit-2":
            with open(dest, "wb") as fh:
                fh.write(b"partial")
            self.blocking_fetch_started.set()
            self.release_fetch.wait(timeout=10)
            self.blocking_fetch_returned.set()
            return True, ""
        assert self.blocking_fetch_started.wait(timeout=2)
        with open(dest, "wb") as fh:
            fh.write(b"staged")
        return True, ""


class _ByteReader(Reader):
    info = PluginInfo(
        name="byte-reader",
        kind="reader",
        summary="test byte reader",
        status=READY,
        instruments=("*",),
        stream_kind=STREAM_COMPLEX_BASEBAND,
    )

    def __init__(self, *, reject_probe: bool = False):
        self.reject_probe = reject_probe

    def probe(self, path):
        if self.reject_probe:
            raise UnreadableUnitError("bad header")
        return {}

    def iter_arrays(self, path, ctx):
        yield b"frame"


class _BuggyProbeReader(_ByteReader):
    def probe(self, path):
        raise ValueError("reader implementation bug")


class _FileAnalyzer(Analyzer):
    info = PluginInfo(
        name="file-analyzer",
        kind="analyzer",
        summary="test file analyzer",
        status=READY,
        instruments=("*",),
        accepts_stream_kinds=(STREAM_COMPLEX_BASEBAND,),
    )

    def __init__(self):
        self.keys: set[str] = set()

    def resume(self, path, ctx):
        return os.path.exists(path)

    def processed_keys(self):
        return self.keys

    def begin(self, ctx, first_meta):
        return None

    def consume_file(self, arrays, meta):
        list(arrays)
        self.keys.add(meta["unit_key"])
        return 1

    def save(self, path):
        with open(path, "w") as fh:
            fh.write("product")

    def summary(self):
        return {"count": len(self.keys)}


class _InvalidCountAnalyzer(_FileAnalyzer):
    def __init__(self, result):
        super().__init__()
        self.result = result

    def consume_file(self, arrays, meta):
        list(arrays)
        return self.result


class _ResumeResultAnalyzer(_FileAnalyzer):
    def __init__(self, result):
        super().__init__()
        self.result = result

    def resume(self, path, ctx):
        return self.result


class _BlockingSaveAnalyzer(_FileAnalyzer):
    def __init__(self):
        super().__init__()
        self.save_started = threading.Event()
        self.release_save = threading.Event()

    def save(self, path):
        self.save_started.set()
        assert self.release_save.wait(timeout=5)
        super().save(path)


class _EarlyStopAnalyzer(_FileAnalyzer):
    def consume_file(self, arrays, meta):
        next(iter(arrays))
        self.keys.add(meta["unit_key"])
        return 1


class _CloseTrackingReader(_ByteReader):
    def __init__(self):
        super().__init__()
        self.closed = False

    def iter_arrays(self, path, ctx):
        with open(path, "rb") as fh:
            try:
                yield fh.read(1)
                yield fh.read(1)
            finally:
                self.closed = True


class _MetadataReader(_ByteReader):
    def probe(self, path):
        return {"f_center_hz": 123.0, "fs_hz": 456.0}


class _MetadataAnalyzer(_FileAnalyzer):
    def __init__(self):
        super().__init__()
        self.meta = None

    def consume_file(self, arrays, meta):
        list(arrays)
        self.meta = dict(meta)
        self.keys.add(meta["unit_key"])
        return 1


def _run(tmp_path, *, source, reader, analyzer=None, units=None, quarantine=None,
         workers=1, staged=1, **run_options):
    return pipeline.run(
        source=source,
        reader=reader,
        analyzer=analyzer or _FileAnalyzer(),
        units=units or [Unit(key="unit-1", name="unit-1.dat")],
        out_path=str(tmp_path / "product.out"),
        tmp_dir=str(tmp_path / "scratch"),
        ctx=RunContext(instrument=object(), options={}),
        quarantine_path=(str(quarantine) if quarantine is not None else None),
        download_workers=workers,
        max_staged_files=staged,
        verbose=False,
        **run_options,
    )


@pytest.mark.parametrize(
    "run_options",
    [
        {"checkpoint_every": 0},
        {"checkpoint_every": True},
        {"workers": 0},
        {"staged": -1},
        {"max_files": 0},
        {"max_frames_per_file": 1.5},
    ],
)
def test_direct_engine_limits_fail_closed(tmp_path, run_options):
    with pytest.raises(ValueError, match="positive integer"):
        _run(
            tmp_path,
            source=_ByteSource(),
            reader=_ByteReader(),
            **run_options,
        )


def test_duplicate_unit_keys_are_rejected_before_fetch(tmp_path):
    source = _ByteSource()
    units = [Unit(key="same", name="one.dat"),
             Unit(key="same", name="two.dat")]

    with pytest.raises(ValueError, match="duplicate unit key"):
        _run(
            tmp_path,
            source=source,
            reader=_ByteReader(),
            units=units,
        )

    assert source.fetch_calls == []


def test_output_lock_excludes_writer_through_final_save(tmp_path):
    """A second run cannot resume stale state while the first is still saving."""
    first_analyzer = _BlockingSaveAnalyzer()
    first_errors = []

    def first_run():
        try:
            _run(
                tmp_path,
                source=_ByteSource(),
                reader=_ByteReader(),
                analyzer=first_analyzer,
            )
        except BaseException as exc:                         # noqa: BLE001
            first_errors.append(exc)

    background = threading.Thread(target=first_run)
    background.start()
    assert first_analyzer.save_started.wait(timeout=2)
    second_source = _ByteSource()
    try:
        with pytest.raises(
                pipeline.OutputLockedError,
                match=r"already locked.*Wait for that run to finish",
        ):
            _run(
                tmp_path,
                source=second_source,
                reader=_ByteReader(),
            )
        assert second_source.fetch_calls == []
    finally:
        first_analyzer.release_save.set()
        background.join(timeout=5)

    assert not background.is_alive()
    assert first_errors == []
    assert (tmp_path / "product.out").read_text() == "product"


def test_existing_product_is_preserved_when_analyzer_reports_no_resume(tmp_path):
    out = tmp_path / "product.out"
    out.write_text("valuable existing product")
    source = _ByteSource()

    with pytest.raises(RuntimeError, match=r"did not resume.*left unchanged"):
        _run(
            tmp_path,
            source=source,
            reader=_ByteReader(),
            analyzer=_ResumeResultAnalyzer(False),
        )

    assert source.fetch_calls == []
    assert out.read_text() == "valuable existing product"


@pytest.mark.parametrize("resume_result", [None, 0, ""])
def test_resume_result_must_be_an_exact_boolean(tmp_path, resume_result):
    source = _ByteSource()

    with pytest.raises(TypeError, match=r"must return exactly True or False"):
        _run(
            tmp_path,
            source=source,
            reader=_ByteReader(),
            analyzer=_ResumeResultAnalyzer(resume_result),
        )

    assert source.fetch_calls == []


def test_download_worker_count_is_clamped_to_staging_capacity_and_work(
        tmp_path, monkeypatch):
    real_thread = threading.Thread
    created = []

    def recording_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        created.append(worker)
        return worker

    monkeypatch.setattr(pipeline.threading, "Thread", recording_thread)
    units = [Unit(key=f"unit-{index}", name=f"unit-{index}.dat")
             for index in range(3)]

    result = _run(
        tmp_path,
        source=_ByteSource(),
        reader=_ByteReader(),
        units=units,
        workers=50,
        staged=2,
    )

    assert result.n_new == 3
    assert len(created) == 2


def test_partial_worker_startup_retains_scratch_under_active_writer(
        tmp_path, monkeypatch):
    source = _BlockingSource()
    # Put the blocking unit first so the only successfully started worker is
    # demonstrably writing when creation of the second worker fails.
    units = [Unit(key="unit-2", name="unit-2.dat"),
             Unit(key="unit-1", name="unit-1.dat")]
    real_start = threading.Thread.start
    calls = 0

    def fail_second_start(worker):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert source.blocking_fetch_started.wait(timeout=2)
            raise RuntimeError("thread capacity exhausted")
        return real_start(worker)

    monkeypatch.setattr(pipeline.threading.Thread, "start", fail_second_start)
    monkeypatch.setattr(pipeline, "_WORKER_JOIN_SECONDS", 0.05)
    scratch = tmp_path / "scratch"
    try:
        with pytest.raises(
                pipeline.ActiveDownloadWorkersError,
                match=r"Scratch was retained",
        ) as caught:
            _run(
                tmp_path,
                source=source,
                reader=_ByteReader(),
                units=units,
                workers=2,
                staged=2,
            )

        assert isinstance(caught.value.__cause__, RuntimeError)
        assert "thread capacity exhausted" in str(caught.value.__cause__)
        assert (scratch / pipeline._stage_name(units[0])).exists()
        assert not (tmp_path / "product.out").exists()
    finally:
        source.release_fetch.set()

    assert source.blocking_fetch_returned.wait(timeout=2)
    deadline = time.monotonic() + 2
    while any(scratch.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("max_frames", [None, 1])
def test_reader_iterator_closes_before_staged_file_delete(
        tmp_path, monkeypatch, max_frames):
    reader = _CloseTrackingReader()
    scratch = tmp_path / "scratch"
    observed_closed = []
    real_remove = pipeline.os.remove

    def observe_remove(path):
        if os.path.dirname(os.path.abspath(path)) == str(scratch):
            observed_closed.append(reader.closed)
        return real_remove(path)

    monkeypatch.setattr(pipeline.os, "remove", observe_remove)
    result = _run(
        tmp_path,
        source=_ByteSource(),
        reader=reader,
        analyzer=_EarlyStopAnalyzer(),
        max_frames_per_file=max_frames,
    )

    assert result.n_new == 1
    assert reader.closed is True
    assert observed_closed == [True]


def test_reader_probe_metadata_is_authoritative_over_inventory(tmp_path):
    analyzer = _MetadataAnalyzer()
    unit = Unit(
        key="unit-1",
        name="unit-1.dat",
        meta={
            "f_center_hz": 999.0,
            "fs_hz": 888.0,
            "inventory_only": "preserved",
        },
    )

    _run(
        tmp_path,
        source=_ByteSource(),
        reader=_MetadataReader(),
        analyzer=analyzer,
        units=[unit],
    )

    assert analyzer.meta["f_center_hz"] == 123.0
    assert analyzer.meta["fs_hz"] == 456.0
    assert analyzer.meta["inventory_only"] == "preserved"


def test_delete_failure_is_surfaced_without_freeing_scratch_slot(
        tmp_path, monkeypatch):
    """An undeleted file remains the sole resident; the next fetch never starts."""
    source = _ByteSource()
    units = [Unit(key="unit-1", name="unit-1.dat"),
             Unit(key="unit-2", name="unit-2.dat")]
    scratch = tmp_path / "scratch"
    real_remove = pipeline.os.remove

    def deny_staged_delete(path):
        if os.path.dirname(os.path.abspath(path)) == str(scratch):
            raise PermissionError("file is busy")
        return real_remove(path)

    monkeypatch.setattr(pipeline.os, "remove", deny_staged_delete)

    with pytest.raises(pipeline.StagedFileCleanupError, match="slot was not released"):
        _run(tmp_path, source=source, reader=_ByteReader(), units=units,
             workers=1, staged=1)

    assert source.fetch_calls == ["unit-1"]
    assert [path.name for path in scratch.iterdir()] == [
        pipeline._stage_name(units[0])
    ]
    assert not (tmp_path / "product.out").exists()


def test_system_exit_in_fetch_is_reported_and_does_not_hang(tmp_path):
    source = _ByteSource(terminal=SystemExit("source requested exit"))

    with pytest.raises(RuntimeError, match=(
            r"download worker terminated.*SystemExit: source requested exit"
    )) as caught:
        _run(tmp_path, source=source, reader=_ByteReader())

    assert isinstance(caught.value.__cause__, SystemExit)
    assert list((tmp_path / "scratch").iterdir()) == []
    assert not (tmp_path / "product.out").exists()


def test_ordinary_fetch_exception_is_not_downgraded_to_file_failure(tmp_path):
    source = _ByteSource(terminal=ValueError("source implementation bug"))

    with pytest.raises(
            RuntimeError,
            match=r"download worker terminated.*ValueError: source implementation bug",
    ) as caught:
        _run(tmp_path, source=source, reader=_ByteReader())

    assert isinstance(caught.value.__cause__, ValueError)
    assert list((tmp_path / "scratch").iterdir()) == []
    assert not (tmp_path / "product.out").exists()


def test_active_fetch_retains_scratch_and_reports_cancellation_timeout(tmp_path):
    source = _BlockingSource()
    units = [Unit(key="unit-1", name="unit-1.dat"),
             Unit(key="unit-2", name="unit-2.dat")]
    scratch = tmp_path / "scratch"
    started = time.monotonic()
    try:
        with pytest.raises(
                pipeline.ActiveDownloadWorkersError,
                match=r"source\.fetch\(\).*Scratch was retained",
        ) as caught:
            _run(
                tmp_path,
                source=source,
                reader=_ByteReader(),
                analyzer=_InvalidCountAnalyzer(0),
                units=units,
                workers=2,
                staged=2,
            )

        assert time.monotonic() - started < 4
        assert caught.value.scratch_dir == str(scratch)
        assert isinstance(caught.value.__cause__, pipeline.AnalyzerConsumptionError)
        assert scratch.is_dir()
        assert (scratch / pipeline._stage_name(units[1])).exists()
    finally:
        source.release_fetch.set()

    assert source.blocking_fetch_returned.wait(timeout=2)
    deadline = time.monotonic() + 2
    while any(scratch.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("invalid_count", [0, -1, None, True, 1.5])
def test_invalid_analyzer_count_does_not_commit_empty_unit(
        tmp_path, invalid_count):
    analyzer = _InvalidCountAnalyzer(invalid_count)

    with pytest.raises(
            pipeline.AnalyzerConsumptionError,
            match=r"must return a positive integer.*not marked done",
    ):
        _run(
            tmp_path,
            source=_ByteSource(),
            reader=_ByteReader(),
            analyzer=analyzer,
        )

    assert analyzer.processed_keys() == set()
    assert not (tmp_path / "product.out").exists()
    assert list((tmp_path / "scratch").iterdir()) == []


def test_all_newly_quarantined_reports_no_product(tmp_path, capsys):
    result = _run(
        tmp_path,
        source=_ByteSource(),
        reader=_ByteReader(reject_probe=True),
        quarantine=tmp_path / "quarantine.jsonl",
    )

    assert result.n_quarantined == 1
    assert result.product_available is False
    assert result.product_written is False
    assert result.resumed is False
    assert not (tmp_path / "product.out").exists()
    assert "product: not created" in capsys.readouterr().out


def test_unclassified_probe_error_aborts_without_quarantine(tmp_path):
    quarantine = tmp_path / "quarantine.jsonl"

    with pytest.raises(RuntimeError, match=(
            r"reader implementation bug.*not quarantined"
    )):
        _run(
            tmp_path,
            source=_ByteSource(),
            reader=_BuggyProbeReader(),
            quarantine=quarantine,
        )

    assert not quarantine.exists()
    assert not (tmp_path / "product.out").exists()
    assert list((tmp_path / "scratch").iterdir()) == []


@pytest.mark.parametrize("existing_product", [False, True])
def test_prequarantined_product_outcome_reflects_resume(
        tmp_path, existing_product, capsys):
    unit = Unit(key="unit-1", name="unit-1.dat")
    quarantine = tmp_path / "quarantine.jsonl"
    pipeline._append_quarantine(str(quarantine), unit, "known bad")
    out = tmp_path / "product.out"
    if existing_product:
        out.write_text("existing product")
    source = _ByteSource()

    result = _run(
        tmp_path,
        source=source,
        reader=_ByteReader(),
        units=[unit],
        quarantine=quarantine,
    )

    assert source.fetch_calls == []
    assert result.n_quarantined == 1
    assert result.product_available is existing_product
    assert result.product_written is False
    assert result.resumed is existing_product
    output = capsys.readouterr().out
    if existing_product:
        assert f"product: {out}" in output
    else:
        assert "no product exists" in output
        assert f"product: {out}" not in output
