"""Focused regressions for archive/plugin state and validation hardening."""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time

import h5py
import numpy as np
import pytest

from datatrawl import instruments
from datatrawl.interfaces import (
    Reader, RunContext, Unit, PluginInfo, SurveyUnavailableError,
    UnreadableUnitError,
)
from datatrawl.plugins.analyzers.spectrum import PowerSpectrumAnalyzer
from datatrawl.plugins.readers.chime_baseband import ChimeBasebandReader
from datatrawl.plugins.readers.outrigger_n2_reader import OutriggerN2Reader
from datatrawl.plugins.sources import _datatrail
from datatrawl.plugins.sources import _cadc_inventory
from datatrawl.plugins.sources import _cadc_transport
from datatrawl.plugins.sources import _survey_state
from datatrawl.plugins.sources import cadc_datatrail as cadc


SCOPE = "chime.event.baseband.raw"
EVENT = "349382977"
COMMON = "cadc:CHIMEFRB/data/raw/2020/01/01/349382977"


def test_event_enumeration_outage_never_replaces_existing_cache(
        monkeypatch, tmp_path):
    cache = tmp_path / "enum_cache.json"
    original = b'{"known":"good"}'
    cache.write_bytes(original)
    monkeypatch.setattr(
        cadc.DATATRAIL, "list_datasets_checked",
        lambda scope: (["dataset-a", "dataset-b"], True),
    )

    def children(scope, dataset):
        if dataset == "dataset-b":
            return [], False
        return [EVENT], True

    monkeypatch.setattr(
        cadc.DATATRAIL, "events_in_dataset_checked", children)
    with pytest.raises(SurveyUnavailableError, match="left untouched"):
        cadc._enumerate_events([SCOPE], False, cache, True)
    assert cache.read_bytes() == original


def test_empty_event_enumeration_cache_remembers_its_scopes(monkeypatch, tmp_path):
    cache = tmp_path / "enum_cache.json"
    calls = []

    def datasets(scope):
        calls.append(scope)
        return [], True

    monkeypatch.setattr(cadc.DATATRAIL, "list_datasets_checked", datasets)
    assert cadc._enumerate_events([SCOPE], False, cache, False) == {}
    payload = json.loads(cache.read_text())
    assert payload["scopes"] == [SCOPE] and payload["events"] == {}
    assert cadc._enumerate_events([SCOPE], False, cache, False) == {}
    assert calls == [SCOPE]


@pytest.mark.parametrize("files", [
    {"file_replica_locations": "not-an-object"},
    {"file_replica_locations": {"minoc": "not-a-list"}},
    {"file_replica_locations": {"minoc": {}}},
    {"file_replica_locations": {"minoc": [123]}},
    {"file_replica_locations": {"minoc": ["cadc:OTHER/path/file.h5"]}},
])
def test_datatrail_rejects_malformed_nested_file_responses(
        monkeypatch, files):
    # Each shape is a deterministic contract violation: the service answered,
    # so it raises the refusal (survey records-and-skips) rather than
    # returning the retried not-answered verdict.
    monkeypatch.setattr(
        _datatrail, "_run_json", lambda args: ({"files": files}, ""))
    with pytest.raises(_datatrail.DatatrailContractError):
        _datatrail.Datatrail().files(SCOPE, EVENT, retries=0)


def test_datatrail_rejects_non_string_listing_entries(monkeypatch):
    monkeypatch.setattr(
        _datatrail, "_run_json", lambda args: ({"larger_datasets": [{}]}, ""))
    assert _datatrail.Datatrail().list_datasets_checked(SCOPE) == ([], False)


class _OneFileReader(Reader):
    survey_schema = 1
    info = PluginInfo(name="one-file", kind="reader", summary="test shape")

    def survey_files(self, event, common_path, selection, ctx):
        yield f"one_{event}.h5", {"kind": "one"}


def _fake_survey_archive(monkeypatch, *, size=10):
    calls = {"enumerate": 0}

    def enumerate_events(*args, **kwargs):
        calls["enumerate"] += 1
        return {(SCOPE, EVENT): ["dataset"]}

    monkeypatch.setattr(cadc, "_enumerate_events", enumerate_events)
    monkeypatch.setattr(
        cadc.DATATRAIL, "common_path",
        lambda scope, event, **kwargs: (COMMON, True))
    monkeypatch.setattr(
        cadc.CadcDatatrailSource, "_cadc_size",
        lambda self, uri, *args, **kwargs: (size, None),
    )
    return calls


def test_manifest_rejects_changed_shape_before_archive_or_state_mutation(
        monkeypatch, tmp_path):
    calls = _fake_survey_archive(monkeypatch)
    source = cadc.CadcDatatrailSource()
    first = RunContext(
        instrument=None, options={"freq_ids": [1]}, reader=_OneFileReader())
    source.survey(first, str(tmp_path))
    before = {
        name: (tmp_path / name).read_bytes()
        for name in ("inventory.jsonl", "attempts.json", "survey_state.sqlite3")
    }

    changed = RunContext(
        instrument=None, options={"freq_ids": [2]}, reader=_OneFileReader())
    with pytest.raises(SystemExit, match="configuration does not match"):
        source.survey(changed, str(tmp_path))
    assert calls["enumerate"] == 1
    assert before == {name: (tmp_path / name).read_bytes() for name in before}


def test_manifest_rejects_changed_reader_size_floor_before_archive_access(
        monkeypatch, tmp_path):
    calls = _fake_survey_archive(monkeypatch, size=10)
    reader = _OneFileReader()
    reader.minimum_archive_bytes = 1
    ctx = RunContext(instrument=None, options={}, reader=reader)
    cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))

    reader.minimum_archive_bytes = 20
    with pytest.raises(SystemExit, match="configuration does not match"):
        cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))
    assert calls["enumerate"] == 1


def test_manifest_rejects_changed_reader_survey_schema_before_archive_access(
        monkeypatch, tmp_path):
    calls = _fake_survey_archive(monkeypatch)
    reader = _OneFileReader()
    ctx = RunContext(instrument=None, options={}, reader=reader)
    cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))

    reader.survey_schema = 2
    with pytest.raises(SystemExit, match="configuration does not match"):
        cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))
    assert calls["enumerate"] == 1


def test_archive_reader_must_declare_survey_schema(tmp_path):
    class MissingSchemaReader(Reader):
        info = PluginInfo(
            name="missing-schema", kind="reader", summary="test shape")

        def survey_files(self, event, common_path, selection, ctx):
            yield "one.h5", {}

    ctx = RunContext(instrument=None, options={}, reader=MissingSchemaReader())
    with pytest.raises(SystemExit, match="must declare.*survey_schema"):
        cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))


def test_transactional_store_recovers_and_deduplicates_event_commit(tmp_path):
    db = tmp_path / "survey_state.sqlite3"
    key = f"{SCOPE}|{EVENT}"
    row = {
        "scope": SCOPE, "event": EVENT, "name": "one.h5", "size_bytes": 10,
        "common_path": COMMON, "obs_date": "2020-01-01", "datasets": [],
    }
    store = _survey_state.SurveyStore(db)
    store.commit(key, SCOPE, EVENT, "complete", [row])
    store.close()  # simulate termination before compatibility views were rendered

    recovered = _survey_state.SurveyStore(db)
    recovered.commit(key, SCOPE, EVENT, "complete", [row])  # idempotent retry
    recovered.render_views(tmp_path)
    recovered.close()
    assert (tmp_path / "surveyed_events.txt").read_text().splitlines() == [key]
    rows = [json.loads(line) for line in
            (tmp_path / "inventory.jsonl").read_text().splitlines()]
    assert rows == [row]


@pytest.mark.parametrize("reader", [
    type("FieldsCollision", (_OneFileReader,), {
        "info": PluginInfo(name="fields-collision", kind="reader", summary="test"),
        "survey_files": lambda self, event, common_path, selection, ctx: iter([
            ("one.h5", {"event": "wrong"})]),
    })(),
    type("AnnotationCollision", (_OneFileReader,), {
        "info": PluginInfo(name="annotation-collision", kind="reader", summary="test"),
        "annotate_row": lambda self, row, instrument: row.__setitem__(
            "scope", "wrong"),
    })(),
])
def test_reader_cannot_overwrite_reserved_inventory_fields(
        monkeypatch, tmp_path, reader):
    _fake_survey_archive(monkeypatch)
    ctx = RunContext(instrument=None, options={}, reader=reader)
    with pytest.raises(SystemExit, match="source-owned inventory field"):
        cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))


def test_reader_archive_candidate_requires_string_name():
    with pytest.raises(SystemExit, match="non-string archive name"):
        _cadc_inventory.candidate_file(
            ({"path": "one.h5"}, {}), "bad-reader")


@pytest.mark.parametrize("name", [
    "../one.h5", "/one.h5", r"folder\one.h5", "folder//one.h5",
    "folder/./one.h5", " one.h5", "one.h5\x00suffix",
])
def test_reader_archive_candidate_requires_canonical_relative_name(name):
    with pytest.raises(SystemExit, match="unsafe archive name"):
        _cadc_inventory.candidate_file((name, {}), "bad-reader")


def _inventory(path, scope, common):
    path.write_text(json.dumps({
        "scope": scope, "event": EVENT, "freq_id": 1,
        "name": "baseband_349382977_1.h5", "common_path": common,
        "size_bytes": 10,
    }) + "\n")


def test_cadc_identity_is_logical_and_quarantine_is_scope_inclusive(tmp_path):
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    c = tmp_path / "c.jsonl"
    _inventory(a, "scope-a", "cadc:CHIMEFRB/old/location")
    _inventory(b, "scope-a", "cadc:CHIMEFRB/new/location")
    _inventory(c, "scope-b", "cadc:CHIMEFRB/old/location")

    def unit(path):
        ctx = RunContext(instrument=None, options={"inventory": str(path)})
        return list(cadc.CadcDatatrailSource().enumerate(ctx))[0]

    ua, ub, uc = unit(a), unit(b), unit(c)
    assert ua.key == ub.key
    assert ua.meta["cadc_uri"] != ub.meta["cadc_uri"]
    assert ua.key != uc.key
    assert ua.meta["quarantine_key"] == ua.key
    assert uc.meta["quarantine_key"] == uc.key


@pytest.mark.parametrize("raw, detail", [
    ("{not-json", "malformed JSON"),
    ("[]", "expected a JSON object"),
    (json.dumps({"scope": "s"}), "missing required field"),
])
def test_inventory_rows_fail_closed_with_location_and_guidance(
        tmp_path, raw, detail):
    path = tmp_path / "inventory.jsonl"
    path.write_text("\n" + raw + "\n")
    ctx = RunContext(instrument=None, options={"inventory": str(path)})
    with pytest.raises(SystemExit) as error:
        list(cadc.CadcDatatrailSource().enumerate(ctx))
    message = str(error.value)
    assert f"{path}:2" in message and detail in message
    assert "datatrawl survey" in message


def test_managed_inventory_name_cannot_escape_root(tmp_path):
    class Instrument:
        name = "chime"

    ctx = RunContext(
        instrument=Instrument(), options={"root": str(tmp_path), "name": "../x"})
    with pytest.raises(SystemExit, match="invalid inventory name"):
        cadc.CadcDatatrailSource()._inventory_path(ctx)


def test_small_products_use_reader_specific_floor(monkeypatch, tmp_path):
    _fake_survey_archive(monkeypatch, size=10)
    ctx = RunContext(instrument=None, options={}, reader=_OneFileReader())
    path = cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))
    assert len((tmp_path / "inventory.jsonl").read_text().splitlines()) == 1
    assert path == str(tmp_path / "inventory.jsonl")


def test_survey_restores_process_socket_default(monkeypatch, tmp_path):
    _fake_survey_archive(monkeypatch)
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(17.0)
    try:
        ctx = RunContext(instrument=None, options={}, reader=_OneFileReader())
        cadc.CadcDatatrailSource().survey(ctx, str(tmp_path))
        assert socket.getdefaulttimeout() == 17.0
    finally:
        socket.setdefaulttimeout(previous)


def test_cadc_http_session_gets_explicit_bounded_timeout():
    class Session:
        def __init__(self):
            self.timeout = None

        def send(self, request, **kwargs):
            self.timeout = kwargs.get("timeout")
            return "response"

    session = Session()
    transport = type("Transport", (), {
        "_get_session": lambda self: session,
    })()
    client = type("Client", (), {"_cadc_client": transport})()

    assert _cadc_transport.configure_request_timeout(client)
    assert session.send(object(), timeout=None) == "response"
    assert session.timeout == _cadc_transport.CADC_REQUEST_TIMEOUT


def test_cadc_fetch_timeout_fallback_is_bounded_and_restored(
        monkeypatch, tmp_path):
    class TimeoutAwareClient:
        def __init__(self):
            self.observed_timeout = None

        def cadcget(self, uri, dest):
            self.observed_timeout = socket.getdefaulttimeout()
            # Model a client waiting for an unresponsive socket. The fallback
            # default supplies the finite wait; the one-second guard keeps a
            # broken regression from hanging the test process indefinitely.
            wait = (self.observed_timeout
                    if self.observed_timeout is not None else 1.0)
            threading.Event().wait(wait)
            raise socket.timeout("simulated stalled CADC transfer")

    timeout = 0.02
    client = TimeoutAwareClient()
    source = cadc.CadcDatatrailSource()
    monkeypatch.setattr(source, "_make_client", lambda: client)
    monkeypatch.setattr(
        _cadc_transport, "CADC_READ_TIMEOUT_SECONDS", timeout)
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(17.0)
    started = time.monotonic()
    try:
        ok, error = source.fetch(
            Unit(key="logical", name="one.h5",
                 meta={"cadc_uri": "cadc:TEST/one.h5"}),
            str(tmp_path / "one.h5"), retries=0, base=0,
        )
        assert socket.getdefaulttimeout() == 17.0
    finally:
        socket.setdefaulttimeout(previous)

    assert not ok and "simulated stalled CADC transfer" in error
    assert client.observed_timeout == timeout
    assert time.monotonic() - started < 0.5


def test_cadc_programming_errors_abort_instead_of_becoming_fetch_failures(
        monkeypatch, tmp_path):
    class BrokenClient:
        def cadcget(self, uri, dest):
            raise AttributeError("simulated source bug")

        def cadcinfo(self, uri):
            raise TypeError("simulated source bug")

    source = cadc.CadcDatatrailSource()
    monkeypatch.setattr(source, "_make_client", BrokenClient)
    unit = Unit(key="logical", name="one.h5",
                meta={"cadc_uri": "cadc:TEST/one.h5"})
    with pytest.raises(AttributeError, match="simulated source bug"):
        source.fetch(unit, str(tmp_path / "one.h5"), retries=0, base=0)
    with pytest.raises(TypeError, match="simulated source bug"):
        source._cadc_size("cadc:TEST/one.h5", retries=0, base=0)


def test_survey_store_rejects_nonnumeric_schema_and_closes(tmp_path):
    path = tmp_path / "survey_state.sqlite3"
    store = _survey_state.SurveyStore(path)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE metadata SET value='broken' WHERE key='schema'")
        connection.commit()
    with pytest.raises(SystemExit, match="invalid survey state schema"):
        _survey_state.SurveyStore(path)
    # Constructor cleanup released the database; it remains immediately usable.
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM metadata").fetchone()[0] == "broken"


def test_manifest_and_attempt_checkpoints_reject_coercible_corruption(tmp_path):
    configuration = {"schema": 1, "scopes": [SCOPE]}
    _survey_state.ensure_manifest(tmp_path, configuration)
    manifest = tmp_path / "survey_manifest.json"
    payload = json.loads(manifest.read_text())
    payload["configuration"]["scopes"] = ["silently-changed"]
    manifest.write_text(json.dumps(payload))
    with pytest.raises(SystemExit, match="manifest is corrupt"):
        _survey_state.ensure_manifest(tmp_path, configuration)

    attempts = tmp_path / "attempts.json"
    for value in (True, 1.5, "1"):
        attempts.write_text(json.dumps({f"{SCOPE}|{EVENT}": value}))
        with pytest.raises(SystemExit, match="non-integer counts"):
            _survey_state.load_attempts(attempts)


def test_survey_store_rejects_wrong_table_shape_and_closes(tmp_path):
    path = tmp_path / "survey_state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (wrong TEXT)")
    with pytest.raises(SystemExit, match="invalid survey state table"):
        _survey_state.SurveyStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='metadata'").fetchone()


def test_spectrum_does_not_record_zero_consumption():
    inst = instruments.load_instrument("chime")
    ctx = RunContext(instrument=inst, selection=[1], options={})
    center = inst.freq_of_freq_id(1) * 1e6
    analyzer = PowerSpectrumAnalyzer()
    analyzer.begin(ctx, {"f_center_hz": center})
    empty_meta = {"unit_key": "empty", "unit_name": "empty.h5",
                  "f_center_hz": center}
    assert analyzer.consume_file(iter(()), empty_meta) == 0
    assert "empty" not in analyzer.processed_keys()

    valid = np.zeros(inst.nfft, dtype=np.complex64)
    assert analyzer.consume_file(iter((valid,)), {
        "unit_key": "valid", "unit_name": "valid.h5", "f_center_hz": center,
    }) == 1
    ragged = np.zeros(inst.nfft // 2, dtype=np.complex64)
    assert analyzer.consume_file(iter((ragged,)), {
        "unit_key": "ragged", "unit_name": "ragged.h5", "f_center_hz": center,
    }) == 0
    assert analyzer.processed_keys() == {"valid"}


def test_baseband_probe_does_not_claim_runtime_nfft(monkeypatch):
    monkeypatch.setattr(
        "datatrawl.plugins.readers.chime_baseband.fmt.channel_center_hz",
        lambda path: 400e6,
    )
    assert "nfft" not in ChimeBasebandReader().probe("unused.h5")


def test_baseband_deterministic_schema_failures_are_unreadable(tmp_path):
    inst = instruments.load_instrument("chime")
    ctx = RunContext(instrument=inst)
    reader = ChimeBasebandReader()

    short = tmp_path / "short.h5"
    empty_feeds = tmp_path / "empty-feeds.h5"
    bad_frequency = tmp_path / "bad-frequency.h5"
    with h5py.File(short, "w") as handle:
        handle.create_dataset(
            "baseband", shape=(inst.nfft - 1, 1), dtype="uint8")
        handle.attrs["freq"] = 400.0
    with h5py.File(empty_feeds, "w") as handle:
        handle.create_dataset(
            "baseband", shape=(inst.nfft, 0), dtype="uint8")
        handle.attrs["freq"] = 400.0
    with h5py.File(bad_frequency, "w") as handle:
        handle.create_dataset(
            "baseband", shape=(inst.nfft, 1), dtype="uint8")
        handle.attrs["freq"] = np.nan

    with pytest.raises(UnreadableUnitError, match="fewer than.*nfft"):
        list(reader.iter_arrays(str(short), ctx))
    with pytest.raises(UnreadableUnitError, match="at least one feed"):
        list(reader.iter_arrays(str(empty_feeds), ctx))
    with pytest.raises(UnreadableUnitError, match="non-finite"):
        reader.probe(str(bad_frequency))


def test_n2_probe_does_not_depend_on_parent_after_staging(tmp_path):
    # The old site/product-count check parsed the staged scratch directory, so
    # it never ran in production. Probe now validates only properties available
    # in the file itself and no longer pretends the parent identifies the site.
    parent = tmp_path / "20250114T093512Z_gbostack_corr"
    parent.mkdir()
    path = parent / "staged.h5"
    time_dtype = np.dtype([("ctime", "f8")])
    with h5py.File(path, "w") as handle:
        handle.create_dataset("vis", data=np.zeros((1024, 3, 1), np.complex64))
        times = np.zeros(1, dtype=time_dtype)
        handle.create_dataset("index_map/time", data=times)
    assert OutriggerN2Reader().probe(str(path))["shape"] == (1024, 3, 1)


def test_spectrum_selection_enforces_instrument_channel_bounds():
    inst = instruments.load_instrument("chime")
    ctx = RunContext(instrument=inst, options={})
    with pytest.raises(SystemExit, match="outside this instrument"):
        PowerSpectrumAnalyzer().resolve_selection(ctx, str(inst.n_channels))
