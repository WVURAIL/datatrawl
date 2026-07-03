#!/usr/bin/env python3
"""
Pins recon's --expand -- born from a field run where `--match gain` found the
right thing (`complex_gains` under gbo.acquisition.processed) but scopes.jsonl
could not take the user further: the hit was a CONTAINER, its children are
timestamped acquisitions (no event IDs), and reaching them required calling
dtcli internals by hand. --expand closes that gap inside recon's charter:

  * with --expand, each kept dataset is opened ONE level and scopes.jsonl
    rows become its children, {scope, dataset: <child>, parent: <container>},
    each directly resolvable with `datatrail ps <scope> <dataset> -s`;
  * a dataset that yields no children keeps its own row (the adapter's []
    means "couldn't determine" as much as "empty" -- nothing found may be
    silently dropped);
  * without --expand, rows and schema are byte-identical to before;
  * both closing messages name the correct next step -- in particular the
    non-expand message no longer tells the user to re-run survey against a
    container survey's event walk cannot see.

All offline: the Datatrail adapter's three listing methods are patched with a
fake landscape shaped like the field run (a gains container per telescope, an
event-keyed scope, one unreadable container).

Run:  PYTHONPATH=src python -m pytest tests/test_recon_expand.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os

import pytest

import datatrawl.cli as cli
from datatrawl.interfaces import RunContext
from datatrawl.plugins.sources import cadc_datatrail
# (the shared DATATRAIL instance is patched via cadc_datatrail; see fake_landscape)

GBO_ACQ = "gbo.acquisition.processed"
KKO_ACQ = "kko.acquisition.processed"
GBO_EVT = "gbo.event.baseband.raw"
TAGS = ["20230530T192643Z_gbo_corr", "20230909T224157Z_gbo_corr",
        "20231009T162555Z_gbo_corr"]

_LANDSCAPE = {
    GBO_ACQ: {"complex_gains": TAGS, "n2_products": ["20230530T192643Z_n2"]},
    KKO_ACQ: {"complex_gains": []},          # unreadable/empty container
    GBO_EVT: {"gains_unrelated_evts": ["349382977", "352918475"]},
}


@contextlib.contextmanager
def fake_landscape():
    """Patch the three listing methods on the SHARED adapter instance.

    Instance-level, not class-level, deliberately: other tests in this suite
    monkeypatch attributes on `cadc_datatrail.DATATRAIL`, and monkeypatch's
    undo re-sets the saved BOUND method as a permanent instance attribute --
    which would shadow a class-level patch made here afterwards. Patching the
    same instance (and deleting on exit anything that was not an instance
    attribute before) is immune to that, and leaves no shadow of our own.
    """
    tgt = cadc_datatrail.DATATRAIL
    fakes = {"list_scopes": lambda: list(_LANDSCAPE),
             "list_datasets": lambda s: list(_LANDSCAPE.get(s, {})),
             "children": lambda s, d: list(_LANDSCAPE.get(s, {}).get(d, []))}
    sentinel = object()
    saved = {n: tgt.__dict__.get(n, sentinel) for n in fakes}
    for n, f in fakes.items():
        setattr(tgt, n, f)
    try:
        yield
    finally:
        for n, old in saved.items():
            if old is sentinel:
                delattr(tgt, n)
            else:
                setattr(tgt, n, old)


def _recon(tmp_path, **options):
    ctx = RunContext(instrument=None, selection=None,
                     options={"scopes_only": True, **options})
    buf = io.StringIO()
    with fake_landscape(), contextlib.redirect_stdout(buf):
        path = cadc_datatrail.CadcDatatrailSource().survey(ctx, str(tmp_path))
    rows = [json.loads(l) for l in open(path) if l.strip()]
    return rows, buf.getvalue()


def test_expand_writes_children_with_parent(tmp_path):
    rows, out = _recon(tmp_path, match="gain", expand=True)
    gains = [r for r in rows if r.get("parent") == "complex_gains"
             and r["scope"] == GBO_ACQ]
    assert [r["dataset"] for r in gains] == TAGS      # the acquisitions, in hand
    # every expanded row is directly `datatrail ps <scope> <dataset>`-able,
    # and the closing message says exactly that
    assert "datatrail ps" in out


def test_expand_keeps_childless_container_row(tmp_path):
    rows, out = _recon(tmp_path, match="gain", expand=True)
    (kko,) = [r for r in rows if r["scope"] == KKO_ACQ]
    assert kko["dataset"] == "complex_gains" and "parent" not in kko
    assert "no children listed" in out                # visible, never dropped


def test_expand_filter_is_name_level_and_scope_wide(tmp_path):
    # 'gain' matches the containers AND the decoy dataset in the event scope
    # (name-level filtering, documented); the event scope's children expand too.
    rows, _ = _recon(tmp_path, match="gain", expand=True)
    evt = [r for r in rows if r["scope"] == GBO_EVT]
    assert {r["dataset"] for r in evt} == {"349382977", "352918475"}
    assert all(r["parent"] == "gains_unrelated_evts" for r in evt)
    # nothing from the unmatched n2 container leaked in
    assert not any(r.get("parent") == "n2_products" for r in rows)


def test_without_expand_schema_and_message_unchanged(tmp_path):
    rows, out = _recon(tmp_path, match="gain")
    assert all(set(r) == {"scope", "dataset"} for r in rows)     # legacy schema
    assert {(r["scope"], r["dataset"]) for r in rows} >= {
        (GBO_ACQ, "complex_gains"), (KKO_ACQ, "complex_gains")}
    assert "--expand" in out           # the honest next step for a container


def test_events_in_dataset_unchanged_through_children_refactor(tmp_path):
    with fake_landscape():
        dt = cadc_datatrail.DATATRAIL         # the instance the fake patches
        assert dt.events_in_dataset(GBO_EVT, "gains_unrelated_evts") == \
            ["349382977", "352918475"]
        assert dt.events_in_dataset(GBO_ACQ, "complex_gains") == []   # no IDs


def test_cli_plumbs_expand(tmp_path):
    with fake_landscape(), contextlib.redirect_stdout(io.StringIO()):
        rc = cli.main(["survey", "--telescope", "gbo", "--scopes-only",
                       "--match", "gain", "--expand", "--root", str(tmp_path)])
    assert rc == 0
    rows = [json.loads(l) for l in
            open(os.path.join(str(tmp_path), "data", "scopes.jsonl"))]
    assert any(r.get("parent") == "complex_gains" for r in rows)


# --------------------------------------------------------------------------
# --telescope on recon: narrows to that telescope's LIVE scopes (first
# component), never to the YAML scopes -- and stays optional for discovery
# --------------------------------------------------------------------------
class _Tel:
    def __init__(self, name):
        self.name = name


def test_telescope_narrows_recon_to_its_live_scopes(tmp_path):
    ctx = RunContext(instrument=_Tel("gbo"), selection=None,
                     options={"scopes_only": True, "expand": True})
    buf = io.StringIO()
    with fake_landscape(), contextlib.redirect_stdout(buf):
        path = cadc_datatrail.CadcDatatrailSource().survey(ctx, str(tmp_path))
    rows = [json.loads(l) for l in open(path) if l.strip()]
    # BOTH gbo scopes kept -- including the acquisition scope the gbo YAML
    # does not declare (the discovery case that motivates live-namespace
    # filtering) -- and nothing from kko/chime
    assert {r["scope"] for r in rows} == {GBO_ACQ, GBO_EVT}
    assert any(r.get("parent") == "complex_gains" for r in rows)
    assert "omit --telescope to walk all" in buf.getvalue()   # the hatch, visible


def test_explicit_scope_wins_over_telescope(tmp_path):
    ctx = RunContext(instrument=_Tel("gbo"), selection=None,
                     options={"scopes_only": True, "scope": KKO_ACQ,
                              "expand": False})
    with fake_landscape(), contextlib.redirect_stdout(io.StringIO()):
        path = cadc_datatrail.CadcDatatrailSource().survey(ctx, str(tmp_path))
    rows = [json.loads(l) for l in open(path) if l.strip()]
    assert {r["scope"] for r in rows} == {KKO_ACQ}


def test_zero_matching_telescope_is_loud(tmp_path):
    ctx = RunContext(instrument=_Tel("dra"), selection=None,   # no dra.* scopes
                     options={"scopes_only": True})
    with fake_landscape(), pytest.raises(SystemExit) as ei:
        cadc_datatrail.CadcDatatrailSource().survey(ctx, str(tmp_path))
    assert "omit --telescope" in str(ei.value).lower()      # never a silent empty map


def test_recon_runs_without_telescope_event_survey_does_not(tmp_path):
    with fake_landscape(), contextlib.redirect_stdout(io.StringIO()):
        rc = cli.main(["survey", "--scopes-only", "--match", "gain",
                       "--root", str(tmp_path)])
    assert rc == 0
    rows = [json.loads(l) for l in
            open(os.path.join(str(tmp_path), "data", "scopes.jsonl"))]
    assert {r["scope"] for r in rows} >= {GBO_ACQ, KKO_ACQ}    # all telescopes
    # the event survey still requires the telescope, with an actionable error
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = cli.main(["survey", "--root", str(tmp_path)])
    assert rc == 2 and "--telescope is required" in err.getvalue()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
