"""Regressions for survey ownership, deadlines, and CADC transfer integrity."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from datatrawl.interfaces import (
    PluginInfo, Reader, RunContext, SurveyUnavailableError, Unit,
)
from datatrawl.plugins.sources import cadc_datatrail as cadc


SCOPE = "chime.event.baseband.raw"
EVENT = "349382977"
COMMON = "cadc:CHIMEFRB/data/raw/2020/01/01/349382977"


class OneFileReader(Reader):
    survey_schema = 1
    info = PluginInfo(name="runtime-one", kind="reader", summary="test shape")

    def survey_files(self, event, common_path, selection, ctx):
        yield f"one_{event}.h5", {"kind": "one"}


class TwoFileReader(OneFileReader):
    info = PluginInfo(name="runtime-two", kind="reader", summary="test shape")

    def survey_files(self, event, common_path, selection, ctx):
        yield "first.h5", {"kind": "first"}
        yield "second.h5", {"kind": "second"}


def _ctx(*, reader=None, freq_ids=None, workers=2):
    return RunContext(
        instrument=None,
        options={"freq_ids": freq_ids or [1], "workers": workers},
        reader=reader or OneFileReader(),
    )


def _patch_archive(monkeypatch, *, size=10):
    monkeypatch.setattr(
        cadc, "_enumerate_events",
        lambda *args, **kwargs: {(SCOPE, EVENT): ["dataset"]},
    )
    monkeypatch.setattr(
        cadc.DATATRAIL, "common_path",
        lambda scope, event, **kwargs: (COMMON, True),
    )
    monkeypatch.setattr(
        cadc.CadcDatatrailSource, "_cadc_size",
        lambda self, uri, *args, **kwargs: (size, None),
    )


def test_active_survey_exclusively_owns_output_and_manifest(
        monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def enumerate_events(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {(SCOPE, EVENT): ["dataset"]}

    monkeypatch.setattr(cadc, "_enumerate_events", enumerate_events)
    monkeypatch.setattr(
        cadc.DATATRAIL, "common_path",
        lambda scope, event, **kwargs: (COMMON, True),
    )
    monkeypatch.setattr(
        cadc.CadcDatatrailSource, "_cadc_size",
        lambda self, uri, *args, **kwargs: (10, None),
    )

    first = _ctx(freq_ids=[1])
    incompatible = _ctx(freq_ids=[2])

    def run_first():
        try:
            cadc.CadcDatatrailSource().survey(first, str(tmp_path))
        except BaseException as exc:  # captured for assertion in the main thread
            errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(SystemExit, match="already in use.*active survey"):
            cadc.CadcDatatrailSource().survey(incompatible, str(tmp_path))
    finally:
        release.set()
        thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    manifest = json.loads((tmp_path / "survey_manifest.json").read_text())
    assert manifest["configuration"]["freq_ids"] == [1]
    with pytest.raises(SystemExit, match="configuration does not match"):
        cadc.CadcDatatrailSource().survey(incompatible, str(tmp_path))


def test_fetch_retries_truncated_file_and_accepts_exact_size(
        monkeypatch, tmp_path):
    class Client:
        calls = 0

        def cadcget(self, uri, dest):
            self.calls += 1
            Path(dest).write_bytes(b"x" if self.calls == 1 else b"x" * 10)

    client = Client()
    source = cadc.CadcDatatrailSource()
    monkeypatch.setattr(source, "_make_client", lambda: client)
    dest = tmp_path / "one.h5"
    ok, error = source.fetch(
        Unit(key="one", name="one.h5",
             meta={"cadc_uri": "cadc:TEST/one.h5", "size_bytes": 10}),
        str(dest), retries=1, base=0,
    )

    assert (ok, error) == (True, "")
    assert client.calls == 2
    assert dest.stat().st_size == 10


def test_fetch_rejects_and_removes_persistently_truncated_file(
        monkeypatch, tmp_path):
    class Client:
        def cadcget(self, uri, dest):
            Path(dest).write_bytes(b"x")

    source = cadc.CadcDatatrailSource()
    monkeypatch.setattr(source, "_make_client", Client)
    dest = tmp_path / "one.h5"
    ok, error = source.fetch(
        Unit(key="one", name="one.h5",
             meta={"cadc_uri": "cadc:TEST/one.h5", "size_bytes": 10}),
        str(dest), retries=0, base=0,
    )

    assert not ok
    assert "expected 10 bytes, received 1" in error
    assert not dest.exists()


def test_survey_waits_for_active_probes_before_returning_error(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        cadc, "_enumerate_events",
        lambda *args, **kwargs: {(SCOPE, EVENT): ["dataset"]},
    )
    monkeypatch.setattr(
        cadc.DATATRAIL, "common_path",
        lambda scope, event, **kwargs: (COMMON, True),
    )
    second_started = threading.Event()
    first_failed = threading.Event()
    release_second = threading.Event()
    errors = []

    def probe(self, uri, *args, **kwargs):
        if uri.endswith("first.h5"):
            assert second_started.wait(5)
            first_failed.set()
            raise AttributeError("probe programming failure")
        second_started.set()
        assert release_second.wait(5)
        return 10, None

    monkeypatch.setattr(cadc.CadcDatatrailSource, "_cadc_size", probe)

    def run():
        try:
            cadc.CadcDatatrailSource().survey(
                _ctx(reader=TwoFileReader()), str(tmp_path))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert second_started.wait(5)
    assert first_failed.wait(5)
    # The exception has occurred, but survey still owns its output and joins
    # the other archive probe before returning it to the caller.
    assert thread.is_alive()
    release_second.set()
    thread.join(5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], AttributeError)


def test_outage_deadline_counts_network_time_and_caps_backoff(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        cadc, "_enumerate_events",
        lambda *args, **kwargs: {(SCOPE, EVENT): ["dataset"]},
    )
    clock = [100.0]
    calls = []
    sleeps = []

    def common_path(scope, event, **kwargs):
        calls.append(kwargs["deadline"])
        clock[0] += 8.0  # time spent inside the network call counts
        return None, False

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(cadc.DATATRAIL, "common_path", common_path)
    monkeypatch.setattr(cadc, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(cadc, "_sleep", sleep)
    monkeypatch.setattr(cadc, "_MAX_SERVICE_WAIT", 10)

    with pytest.raises(SurveyUnavailableError, match="unreachable for 10s"):
        cadc.CadcDatatrailSource().survey(_ctx(), str(tmp_path))

    assert calls == [110.0]
    assert sleeps == [2.0]


def test_survey_does_not_mutate_socket_default_around_nonnetwork_work(
        monkeypatch, tmp_path):
    observed = []

    def enumerate_events(*args, **kwargs):
        observed.append(socket.getdefaulttimeout())
        return {(SCOPE, EVENT): ["dataset"]}

    monkeypatch.setattr(cadc, "_enumerate_events", enumerate_events)
    monkeypatch.setattr(
        cadc.DATATRAIL, "common_path",
        lambda scope, event, **kwargs: (COMMON, True),
    )
    monkeypatch.setattr(
        cadc.CadcDatatrailSource, "_cadc_size",
        lambda self, uri, *args, **kwargs: (10, None),
    )
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(17.0)
    try:
        cadc.CadcDatatrailSource().survey(_ctx(), str(tmp_path))
    finally:
        socket.setdefaulttimeout(previous)

    assert observed == [17.0]


def test_legacy_socket_fallback_rechecks_deadline_after_lock_wait(
        monkeypatch):
    clock = [5.0]

    class AdvanceClockLock:
        def __enter__(self):
            clock[0] = 11.0

        def __exit__(self, exc_type, exc, traceback):
            return False

    class Client:
        calls = 0

        def cadcinfo(self, uri):
            self.calls += 1
            raise AssertionError("request started after its deadline")

    client = Client()
    source = cadc.CadcDatatrailSource()
    monkeypatch.setattr(source, "_make_client", lambda: client)
    monkeypatch.setattr(
        cadc._cadc_transport, "_SOCKET_DEFAULT_LOCK", AdvanceClockLock())
    monkeypatch.setattr(
        cadc._cadc_transport.time, "monotonic", lambda: clock[0])

    size, error = source._cadc_size(
        "cadc:TEST/one.h5", retries=0, deadline=10.0)

    assert size is None
    assert isinstance(error, TimeoutError)
    assert client.calls == 0
