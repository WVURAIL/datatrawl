#!/usr/bin/env python3
"""
Pins the two contracts added for event-oriented analyses (the first external
use case: beamforming every freq_id of ONE event into a single product):

  1. SELECTION -- `ctx.selection` now carries events as well as freq_ids, via
     one shared grammar (`plugins/sources/_selection.py`). The legacy freq_id
     forms parse exactly as before; an event filter is always explicit
     ('events:...' prefix or a {"events": ...} dict) and never inferred from
     the magnitude of a bare integer. Both archive and local sources apply the
     same ANDed, exact-match semantics.

  2. SHAPE -- the archive file shape (which files one event contributes, and
     their names) lives on the READER (Reader.survey_files), not inside the
     CADC source. survey() writes each verified file's `name` into its row, so
     enumerate() stages exactly what survey verified -- no naming
     re-derivation, no drift. Legacy rows (no `name`) still reconstruct the
     baseband filename, so pre-existing inventories keep scanning.

Everything here is offline: enumerate reads a synthetic inventory.jsonl, and
the survey test injects fakes for the same three seams
tests/test_survey_empty_events.py patches (enumerate-events, `datatrail ps`,
cadcinfo).

Run:  PYTHONPATH=src python -m pytest tests/test_event_selection_and_shape.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os

import pytest

import datatrawl.cli as cli
from datatrawl.interfaces import Reader, RunContext, PluginInfo, READY
from datatrawl.plugins.sources import cadc_datatrail
from datatrawl.plugins.sources._selection import parse_selection
from datatrawl.plugins.sources.local import LocalDirectorySource
from datatrawl.plugins.readers.chime_baseband import baseband_filename

EV_A, EV_B = "349382977", "352918475"
CP = "cadc:TEST/fixture/2020/01/01/x"


# --------------------------------------------------------------------------
# 1) the shared grammar
# --------------------------------------------------------------------------
@pytest.mark.parametrize("spec, freq_ids, events", [
    (None,                 None,                None),
    ("all",                None,                None),
    ("*",                  None,                None),
    (844,                  {844},               None),
    ([614, 706],           {614, 706},          None),
    ([],                   None,                None),          # empty == all
    ("614,706",            {614, 706},          None),
    ("506-508",            {506, 507, 508},     None),
    (f"events:{EV_A}",     None,                {EV_A}),
    (f"EVENT:{EV_A}",      None,                {EV_A}),        # case-insensitive
    (f"events:{EV_A},{EV_B}", None,             {EV_A, EV_B}),
    ({"events": [EV_A]},   None,                {EV_A}),
    ({"events": int(EV_B)}, None,               {EV_B}),        # int event ok
    ({"events": [EV_A], "freq_ids": "614,706"}, {614, 706}, {EV_A}),
])
def test_parse_selection_grammar(spec, freq_ids, events):
    sel = parse_selection(spec)
    assert sel.freq_ids == (frozenset(freq_ids) if freq_ids else None)
    assert sel.events == (frozenset(events) if events else None)


def test_parse_selection_fails_loud():
    # A typoed dict key or an empty events: prefix must not silently select
    # nothing -- that is a plan_runs bug the author needs to see.
    with pytest.raises(SystemExit):
        parse_selection({"event": EV_A})          # singular key: typo
    with pytest.raises(SystemExit):
        parse_selection("events:")


@pytest.mark.parametrize("bad", [
    "foo",                                  # not a freq_id at all
    "506-844-900",                          # malformed range
    "614,x",                                # one bad token in a list
    ["614", "x"],                           # collection with a bad element
    {"freq_ids": "506x"},                   # via the dict form
])
def test_malformed_freq_ids_are_actionable(bad):
    # int() tracebacks are the failure mode this pins against: a plan_runs
    # typo must name itself as a selection error, not a ValueError 3 frames in.
    with pytest.raises(SystemExit) as ei:
        parse_selection(bad)
    assert "freq_id" in str(ei.value)


def test_reversed_range_is_loud_not_select_all():
    # '844-506' used to parse to an empty set == "no filter" == EVERYTHING --
    # the one typo that turns a one-channel scan into an archive pull.
    with pytest.raises(SystemExit) as ei:
        parse_selection("844-506")
    assert "844" in str(ei.value) and "506" in str(ei.value)


def test_event_selection_in_freq_id_slot_names_the_mistake():
    with pytest.raises(SystemExit) as ei:
        parse_selection({"freq_ids": f"events:{EV_A}"})
    assert "events" in str(ei.value)


def test_exact_match_semantics():
    sel = parse_selection({"freq_ids": [614]})
    assert sel.wants_freq_id(614) and not sel.wants_freq_id(44)
    assert not sel.wants_freq_id(None)     # unit without the concept: excluded
    sel = parse_selection(f"events:{EV_A}")
    assert sel.wants_event(EV_A) and sel.wants_event(int(EV_A))
    assert not sel.wants_event(None)


# --------------------------------------------------------------------------
# 2) archive enumerate: filters + self-describing rows + legacy fallback
# --------------------------------------------------------------------------
def _write_inventory(d, rows):
    p = os.path.join(d, "inventory.jsonl")
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def _units(inv_path, selection):
    ctx = RunContext(instrument=None, selection=selection,
                     options={"inventory": inv_path})
    # instrument=None is fine: enumerate never touches it when --inventory is
    # explicit (only the default-path derivation needs a telescope name).
    return list(cadc_datatrail.CadcDatatrailSource().enumerate(ctx))


@pytest.fixture()
def mixed_inventory(tmp_path):
    """A legacy baseband row (no `name`), a new-style baseband row, and a
    per-event calibration-style row (has `name`, has NO freq_id)."""
    rows = [
        {"scope": "s", "event": EV_A, "freq_id": 614, "size_bytes": 10,
         "common_path": CP, "obs_date": "2020-01-01"},                 # legacy
        {"scope": "s", "event": EV_A, "freq_id": 706, "size_bytes": 11,
         "name": baseband_filename(EV_A, 706),
         "common_path": CP, "obs_date": "2020-01-01"},                 # new
        {"scope": "s", "event": EV_B, "name": f"gains_{EV_B}.h5",
         "kind": "gains", "size_bytes": 12,
         "common_path": CP, "obs_date": "2020-01-02"},                 # no freq_id
    ]
    return _write_inventory(str(tmp_path), rows)


def test_enumerate_legacy_row_reconstructs_baseband_name(mixed_inventory):
    (u,) = [u for u in _units(mixed_inventory, None) if u.meta.get("freq_id") == 614]
    assert u.name == baseband_filename(EV_A, 614)
    assert u.key == f"{CP}/{u.name}"
    assert u.meta["quarantine_key"] == f"{EV_A}:614"


def test_enumerate_row_name_is_authoritative(mixed_inventory):
    (u,) = [u for u in _units(mixed_inventory, None) if u.meta.get("kind") == "gains"]
    assert u.name == f"gains_{EV_B}.h5"
    assert u.key == f"{CP}/gains_{EV_B}.h5"
    # shape-specific columns ride through meta untouched; quarantine identity
    # falls back to event:name when there is no freq_id
    assert u.meta["event"] == EV_B and u.meta["obs_date"] == "2020-01-02"
    assert u.meta["quarantine_key"] == f"{EV_B}:gains_{EV_B}.h5"
    assert "freq_id" not in u.meta


def test_enumerate_event_and_freq_filters(mixed_inventory):
    assert {u.meta["event"] for u in _units(mixed_inventory, f"events:{EV_A}")} == {EV_A}
    # a freq_id filter excludes the row that has no freq_id concept at all
    assert all(u.meta.get("freq_id") == 706
               for u in _units(mixed_inventory, "706"))
    combo = _units(mixed_inventory, {"events": [EV_A], "freq_ids": [614]})
    assert len(combo) == 1 and combo[0].meta["freq_id"] == 614
    # legacy behavior is byte-identical for the all-selection
    assert len(_units(mixed_inventory, None)) == 3


# --------------------------------------------------------------------------
# 3) local source parity
# --------------------------------------------------------------------------
def test_local_source_event_selection(tmp_path):
    for ev, ch in ((EV_A, 614), (EV_A, 706), (EV_B, 614)):
        (tmp_path / f"baseband_{ev}_{ch}.h5").write_bytes(b"x")
    ctx = RunContext(instrument=None, selection=f"events:{EV_A}",
                     options={"source_root": str(tmp_path)})
    units = list(LocalDirectorySource().enumerate(ctx))
    assert {u.meta["event"] for u in units} == {EV_A} and len(units) == 2
    ctx.selection = {"events": [EV_B], "freq_ids": [614]}
    (u,) = list(LocalDirectorySource().enumerate(ctx))
    assert u.meta["event"] == EV_B and u.meta["freq_id"] == 614


def test_local_source_custom_event_regex(tmp_path):
    (tmp_path / f"cal_{EV_A}.h5").write_bytes(b"x")
    ctx = RunContext(instrument=None, selection=f"events:{EV_A}",
                     options={"source_root": str(tmp_path),
                              "source_event_regex": r"cal_(\d+)\.h5$"})
    (u,) = list(LocalDirectorySource().enumerate(ctx))
    assert u.meta["event"] == EV_A


# --------------------------------------------------------------------------
# 4) survey with a reader-owned shape: verified name -> row -> enumerate
# --------------------------------------------------------------------------
class GainsShapeReader(Reader):
    """A minimal per-event product shape: ONE file per event, no freq_id --
    the kind of external reader a calibration-product survey would register."""
    info = PluginInfo(name="test-gains", kind="reader",
                      summary="per-event gains (test shape)", status=READY)

    def survey_files(self, event, common_path, selection, ctx):
        yield f"gains_{event}.h5", {"kind": "gains"}


@contextlib.contextmanager
def fake_archive(events):
    """Patch the three live seams survey() uses (same pattern as
    tests/test_survey_empty_events.py); every candidate file 'exists' with an
    above-floor size."""
    orig_enum = cadc_datatrail._enumerate_events
    orig_ps = cadc_datatrail.DATATRAIL.common_path
    orig_size = cadc_datatrail.CadcDatatrailSource._cadc_size
    cadc_datatrail._enumerate_events = lambda *a, **k: {
        ("s", ev): ["ds"] for ev in events}
    cadc_datatrail.DATATRAIL.common_path = lambda scope, ev: (
        f"cadc:TEST/data/raw/2020/01/01/{ev}", True)
    cadc_datatrail.CadcDatatrailSource._cadc_size = (
        lambda self, uri, *a, **k: (cadc_datatrail._MIN_VALID_BYTES + 1, None))
    try:
        yield
    finally:
        cadc_datatrail._enumerate_events = orig_enum
        cadc_datatrail.DATATRAIL.common_path = orig_ps
        cadc_datatrail.CadcDatatrailSource._cadc_size = orig_size


def test_survey_uses_reader_shape_and_roundtrips(tmp_path):
    ctx = RunContext(instrument=None, selection=None, options={},
                     reader=GainsShapeReader())
    with fake_archive([EV_A]):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            inv = cadc_datatrail.CadcDatatrailSource().survey(ctx, str(tmp_path))
    assert "shape=test-gains" in buf.getvalue()
    rows = [json.loads(l) for l in open(inv) if l.strip()]
    assert len(rows) == 1
    (r,) = rows
    assert r["name"] == f"gains_{EV_A}.h5" and r["kind"] == "gains"
    assert "freq_id" not in r
    # the row is self-describing: enumerate stages EXACTLY what was verified
    (u,) = _units(inv, None)
    assert u.name == r["name"] and u.key.endswith(f"/{r['name']}")
    assert u.meta["kind"] == "gains"


def test_survey_without_reader_falls_back_to_baseband_shape(tmp_path):
    ctx = RunContext(instrument=None, selection=None,
                     options={"freq_ids": [614, 706]})
    with fake_archive([EV_A]):
        with contextlib.redirect_stdout(io.StringIO()):
            inv = cadc_datatrail.CadcDatatrailSource().survey(ctx, str(tmp_path))
    rows = sorted((json.loads(l) for l in open(inv) if l.strip()),
                  key=lambda r: r["freq_id"])
    assert [r["name"] for r in rows] == [baseband_filename(EV_A, 614),
                                         baseband_filename(EV_A, 706)]
    assert [r["freq_id"] for r in rows] == [614, 706]


# --------------------------------------------------------------------------
# 5) product naming for the structured (per-event) sub-selection
# --------------------------------------------------------------------------
def test_default_product_path_for_event_selection(tmp_path):
    class A:                       # what _default_product_path reads from args
        root, analyzer = str(tmp_path), "beam"
    class I:
        name = "chime"
    p = cli._default_product_path(A, I, {"events": [EV_A]})
    assert p.endswith(os.path.join("results", "chime", "beam",
                                   f"ev{EV_A}.npz"))
    p = cli._default_product_path(A, I, {"events": [EV_A], "freq_ids": "506-508"})
    assert os.path.basename(p) == f"ev{EV_A}_506-508.npz"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
