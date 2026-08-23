#!/usr/bin/env python3
"""Regression test for the survey stall on a deterministic Datatrail refusal.

Field signature (chime-controls survey, 2026-08): one event whose minoc
replica sat outside the expected 'cadc:CHIMEFRB/' collection was classified
as "service unreachable", so the survey retried the SAME event with capped
backoff and would then have aborted the entire 10k-event run at
_MAX_SERVICE_WAIT with a misleading renew-your-certificate message.

The fix separates the verdicts: a payload the adapter cannot act on raises
DatatrailContractError (deterministic, never retried), and survey() commits
the event as `refused`, ledgers the reason -- including the offending URI --
in no_files_events.jsonl, and moves on. These tests pin:

  * files() raises for an out-of-collection replica, naming the URI;
  * files() raises for replicas spanning two collections;
  * the transient error envelope still returns the retried not-answered
    verdict, unchanged;
  * a survey with one refused and one good event completes, writes the good
    event's rows, commits the refused event (resume skips it), and ledgers
    the refusal reason verbatim.

Run:  PYTHONPATH=src python -m pytest tests/test_survey_contract_refusal.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile

import pytest

from datatrawl.interfaces import RunContext
from datatrawl.plugins.readers.chime_baseband import MINIMUM_ARCHIVE_BYTES
from datatrawl.plugins.sources import cadc_datatrail
from datatrawl.plugins.sources import _datatrail as dt
from datatrawl.plugins.sources._datatrail import (Datatrail,
                                                  DatatrailContractError)

SCOPE = "chime.event.baseband.raw"
REFUSED_EVENT = "100260502"
GOOD_EVENT = "900000009"
FREQ_IDS = [614, 706]
STRAY_URI = "cadc:CHIMEB/raw/2024/01/01/100260502/baseband_100260502_614.h5"


# ---------------------------------------------------------------- files()
def _payload(uris):
    return {"files": {"file_replica_locations": {"minoc": list(uris)}}}


def _files_with(monkeypatch, payload):
    monkeypatch.setattr(dt, "_run_json", lambda *a, **k: (payload, ""))
    return Datatrail().files(SCOPE, REFUSED_EVENT, retries=0, base=0.0)


def test_out_of_collection_replica_raises_and_names_the_uri(monkeypatch):
    with pytest.raises(DatatrailContractError) as err:
        _files_with(monkeypatch, _payload([STRAY_URI]))
    msg = str(err.value)
    assert STRAY_URI in msg and "cadc:CHIMEFRB/" in msg


def test_mixed_collections_raise(monkeypatch):
    dt_local = dt._MINOC_COLLECTIONS
    monkeypatch.setattr(dt, "_MINOC_COLLECTIONS",
                        dt_local + ("cadc:CHIMEB/",))
    with pytest.raises(DatatrailContractError) as err:
        _files_with(monkeypatch, _payload(
            ["cadc:CHIMEFRB/raw/x/a.h5", "cadc:CHIMEB/raw/x/b.h5"]))
    assert "multiple collections" in str(err.value)


def test_error_envelope_stays_transient(monkeypatch):
    monkeypatch.setattr(dt, "_run_json",
                        lambda *a, **k: ({"error": "boom"}, ""))
    cp, names, ok = Datatrail().files(SCOPE, REFUSED_EVENT,
                                      retries=0, base=0.0)
    assert (cp, names, ok) == (None, [], False)


# ---------------------------------------------------------------- survey()
@contextlib.contextmanager
def fake_archive():
    """Patch survey()'s three seams: one refused event, one good one."""
    orig_enum = cadc_datatrail._enumerate_events
    orig_ps = cadc_datatrail.DATATRAIL.common_path
    orig_size = cadc_datatrail.CadcDatatrailSource._cadc_size

    membership = {(SCOPE, REFUSED_EVENT): ["ds"], (SCOPE, GOOD_EVENT): ["ds"]}

    def fake_common_path(scope, ev, **kwargs):
        if ev == REFUSED_EVENT:
            raise DatatrailContractError(
                f"[datatrail ps {scope} {ev}] minoc replica {STRAY_URI!r} "
                "outside the expected collection(s) ['cadc:CHIMEFRB/'] "
                "(1/1 replicas affected)")
        return f"cadc:CHIMEFRB/data/raw/2020/01/01/{ev}", True

    def fake_size(self, uri, *a, **k):
        return MINIMUM_ARCHIVE_BYTES + 1, None

    cadc_datatrail._enumerate_events = lambda *a, **k: dict(membership)
    cadc_datatrail.DATATRAIL.common_path = fake_common_path
    cadc_datatrail.CadcDatatrailSource._cadc_size = fake_size
    try:
        yield
    finally:
        cadc_datatrail._enumerate_events = orig_enum
        cadc_datatrail.DATATRAIL.common_path = orig_ps
        cadc_datatrail.CadcDatatrailSource._cadc_size = orig_size


def _survey(out_dir):
    ctx = RunContext(instrument=None, selection=None,
                     options={"freq_ids": list(FREQ_IDS)})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        src = cadc_datatrail.CadcDatatrailSource()
        src.survey(ctx, out_dir)
    return buf.getvalue(), src


def _rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def test_refused_event_is_ledgered_and_survey_completes():
    with tempfile.TemporaryDirectory() as out, fake_archive():
        text, src = _survey(out)
        # The good event's rows landed; the refusal did not stall or abort.
        rows = _rows(os.path.join(out, "inventory.jsonl"))
        assert {r["event"] for r in rows} == {GOOD_EVENT}
        assert len(rows) == len(FREQ_IDS)
        assert "contract refusal -- recorded and skipped" in text
        assert "1 contract-refused" in text
        assert "service unreachable" not in text
        # Ledger entry carries the reason with the offending URI, verbatim.
        ledger = _rows(os.path.join(out, "no_files_events.jsonl"))
        refusals = [r for r in ledger
                    if r.get("reason") == "datatrail-contract-refusal"]
        assert len(refusals) == 1
        assert refusals[0]["event"] == REFUSED_EVENT
        assert STRAY_URI in refusals[0]["detail"]
        assert src.survey_completeness_issues(out) == {
            "incomplete": 0, "refused": 1, "pending": 0,
        }
        # Committed: a second pass resumes past it and re-does nothing.
        text2, resumed_src = _survey(out)
        assert "resume: 2 events already done" in text2
        assert "0 contract-refused" in text2
        assert resumed_src.survey_completeness_issues(out)["refused"] == 1
