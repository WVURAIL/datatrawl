#!/usr/bin/env python3
"""
Regression test for the silent "resolved-but-empty" survey failure.

The bug: survey() resolved a Datatrail Common Path for an event but every
requested freq_id came back absent (cadcinfo NotFound) or under the size floor.
That yields zero records AND zero hard errors, which _commit_decision read as
"clean" -- so the event was marked permanently done and ZERO rows were written,
while the run still reported `survey wrote <path>`. The tell in the field was a
run that processed N events and printed not one per-event line (those only fire
when records or errored is non-empty) and left inventory.jsonl at 0 lines while
surveyed_events.txt filled.

These tests pin the fix WITHOUT any CADC/Datatrail access by injecting fakes for
the three seams survey() leans on -- enumerate, `datatrail ps`, and cadcinfo:

  * _commit_decision now distinguishes 0-record-0-error (empty) from clean;
  * an empty event is NOT marked done on the first pass -- it is retried across
    resumes, so a rerun picks it up;
  * after _MAX_ATTEMPTS it is accepted-as-empty: marked done AND recorded in
    no_files_events.txt (visible, never silent, never re-probed forever);
  * a run whose inventory ends at 0 rows prints a loud [warn]; and
  * the happy path (freq_ids present -> rows written, event done) still works.

Run:  PYTHONPATH=src python tests/test_survey_empty_events.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

import pytest

import datatrawl.cli as cli
from datatrawl.plugins.sources import cadc_datatrail
from datatrawl.interfaces import RunContext

SCOPE = "chime.event.baseband.raw"
ABSENT_EVENT = "100000001"          # cp resolves, every freq_id NotFound
PRESENT_EVENT = "900000009"         # cp resolves, every freq_id present + big
FREQ_IDS = [614, 706]
ABOVE_FLOOR = cadc_datatrail._MIN_VALID_BYTES + 1


@contextlib.contextmanager
def fake_archive(membership, present_events):
    """Patch the three live seams survey() uses, restore them on exit."""
    orig_enum = cadc_datatrail._enumerate_events
    orig_ps = cadc_datatrail.DATATRAIL.common_path
    orig_size = cadc_datatrail.CadcDatatrailSource._cadc_size
    present = set(present_events)

    def fake_size(self, uri, *a, **k):
        # PRESENT events -> an above-floor size; everything else -> NotFound,
        # which the real _cadc_size reports as (None, None): the silent-absent
        # case that drove the bug.
        if any(ev in str(uri) for ev in present):
            return ABOVE_FLOOR, None
        return None, None

    cadc_datatrail._enumerate_events = lambda *a, **k: dict(membership)
    cadc_datatrail.DATATRAIL.common_path = (
        lambda scope, ev: (f"cadc:CHIMEFRB/data/raw/2020/01/01/{ev}", True))
    cadc_datatrail.CadcDatatrailSource._cadc_size = fake_size
    try:
        yield
    finally:
        cadc_datatrail._enumerate_events = orig_enum
        cadc_datatrail.DATATRAIL.common_path = orig_ps
        cadc_datatrail.CadcDatatrailSource._cadc_size = orig_size


def _survey(out_dir):
    """Run one survey pass over out_dir; return (returned_path, stdout)."""
    ctx = RunContext(instrument=None, selection=None,
                     options={"freq_ids": list(FREQ_IDS)})
    src = cadc_datatrail.CadcDatatrailSource()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        src.survey(ctx, out_dir)
    return buf.getvalue()


def _lines(path):
    return [l.strip() for l in open(path)] if os.path.exists(path) else []


def _rows(path):
    return [json.loads(l) for l in _lines(path) if l]


def _state(out_dir):
    return {
        "rows": _rows(os.path.join(out_dir, "inventory.jsonl")),
        "surveyed": [l for l in _lines(os.path.join(out_dir, "surveyed_events.txt")) if l],
        "no_files": [l for l in _lines(os.path.join(out_dir, "no_files_events.txt")) if l],
    }


# --------------------------------------------------------------------------
# 1) pure decision function
# --------------------------------------------------------------------------
def run_commit_decision_unit() -> int:
    cd = cadc_datatrail._commit_decision
    M = cadc_datatrail._MAX_ATTEMPTS
    # (label, got, want) for (write_records, mark_done, incomplete, made_progress)
    cases = [
        ("clean (rows)",      cd(0, 4, 0, n_records=4),     (True, True, False, True)),
        ("partial retry",     cd(2, 4, 0, n_records=2),     (False, False, False, True)),
        ("partial accept",    cd(2, 4, M - 1, n_records=2), (True, True, True, True)),
        ("empty retry",       cd(0, 4, 0, n_records=0),     (False, False, False, True)),
        ("empty accept",      cd(0, 4, M - 1, n_records=0), (False, True, False, True)),
        ("compat (no n_records)", cd(0, 4, 0),              (True, True, False, True)),
    ]
    ok = True
    for label, got, want in cases:
        if got != want:
            print(f"  FAIL: _commit_decision {label}: got {got}, want {want}")
            ok = False
    # the crux: empty must NOT mark done while attempts remain, and clean must.
    if cd(0, 4, 0, n_records=0)[1] is not False:
        print("  FAIL: empty-with-attempts-left was marked done"); ok = False
    if cd(0, 4, 0, n_records=4)[1] is not True:
        print("  FAIL: clean event was not marked done"); ok = False
    print("  _commit_decision: clean / partial / empty verdicts all correct")
    print("COMMIT-DECISION UNIT PASSED" if ok else "COMMIT-DECISION UNIT FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 2) survey() end-to-end: empty event coexists with a real one
# --------------------------------------------------------------------------
def run_mixed_survey() -> int:
    print("MIXED SURVEY (one present event, one all-absent event)")
    work = tempfile.mkdtemp(prefix="dtw_mixed_")
    ok = True
    try:
        membership = {
            (SCOPE, ABSENT_EVENT): ["dataset_a"],
            (SCOPE, PRESENT_EVENT): ["dataset_a"],
        }
        out_dir = os.path.join(work, "data", "chime-test")
        with fake_archive(membership, present_events=[PRESENT_EVENT]):
            log = _survey(out_dir)
        st = _state(out_dir)
        present_key, absent_key = f"{SCOPE}|{PRESENT_EVENT}", f"{SCOPE}|{ABSENT_EVENT}"

        # present event -> 2 rows (one per freq_id), all for the present event
        if len(st["rows"]) != len(FREQ_IDS):
            print(f"  FAIL: expected {len(FREQ_IDS)} rows, got {len(st['rows'])}")
            ok = False
        if any(r["event"] != PRESENT_EVENT for r in st["rows"]):
            print("  FAIL: a row was written for the absent event"); ok = False
        # present event marked done; absent event NOT (it must be retried)
        if present_key not in st["surveyed"]:
            print("  FAIL: present event was not marked done"); ok = False
        if absent_key in st["surveyed"]:
            print("  FAIL: absent event was silently marked done (the bug)")
            ok = False
        # not yet accepted as empty (first pass only)
        if st["no_files"]:
            print(f"  FAIL: no_files written too early: {st['no_files']}"); ok = False
        # the absent event is now visible in the log, not invisible
        if ABSENT_EVENT not in log or "retry" not in log.lower():
            print("  FAIL: absent event produced no visible per-event line"); ok = False
        print(f"  present -> {len(st['rows'])} rows + done; absent -> retried, "
              f"not done, logged")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("MIXED SURVEY PASSED" if ok else "MIXED SURVEY FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 3) survey() end-to-end: a wholly-empty survey warns, then accepts after retries
# --------------------------------------------------------------------------
def run_empty_survey() -> int:
    print("EMPTY SURVEY (only an all-absent event; reproduces the report)")
    work = tempfile.mkdtemp(prefix="dtw_empty_")
    ok = True
    try:
        membership = {(SCOPE, ABSENT_EVENT): ["dataset_a"]}
        out_dir = os.path.join(work, "data", "chime-test")
        absent_key = f"{SCOPE}|{ABSENT_EVENT}"
        M = cadc_datatrail._MAX_ATTEMPTS

        with fake_archive(membership, present_events=[]):
            log1 = _survey(out_dir)
            st1 = _state(out_dir)

            # run 1: 0 rows, loud warning, event NOT marked done
            if st1["rows"]:
                print(f"  FAIL: empty survey wrote {len(st1['rows'])} rows"); ok = False
            if "inventory.jsonl is EMPTY" not in log1:
                print("  FAIL: empty inventory did not raise the [warn]"); ok = False
            if absent_key in st1["surveyed"]:
                print("  FAIL: empty event marked done on first pass"); ok = False

            # runs up to _MAX_ATTEMPTS: it gets accepted-as-empty (done + logged)
            log_last = log1
            for _ in range(M - 1):
                log_last = _survey(out_dir)
            st = _state(out_dir)

        if absent_key not in st["surveyed"]:
            print("  FAIL: empty event never accepted/marked done after "
                  f"{M} attempts (would re-probe forever)"); ok = False
        if absent_key not in st["no_files"]:
            print("  FAIL: accepted-empty event not recorded in no_files_events.txt")
            ok = False
        if st["rows"]:
            print("  FAIL: rows appeared for an all-absent event"); ok = False
        if "accepting as empty" not in log_last:
            print("  FAIL: acceptance was not announced in the log"); ok = False
        print(f"  run 1: 0 rows + [warn] + not-done; after {M} runs: "
              f"accepted-empty, done, in no_files")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("EMPTY SURVEY PASSED" if ok else "EMPTY SURVEY FAILED")
    return 0 if ok else 1


# -- pytest entry points ---------------------------------------------------
def test_commit_decision_unit():
    assert run_commit_decision_unit() == 0


def test_mixed_survey():
    assert run_mixed_survey() == 0


def test_empty_survey():
    assert run_empty_survey() == 0


def test_sustained_service_outage_is_nonzero_and_preserves_state(
        monkeypatch, tmp_path, capsys):
    membership = {(SCOPE, ABSENT_EVENT): ["dataset_a"]}
    monkeypatch.setattr(
        cadc_datatrail, "_enumerate_events", lambda *a, **k: dict(membership)
    )
    monkeypatch.setattr(
        cadc_datatrail.DATATRAIL, "common_path",
        lambda scope, event: (None, False),
    )
    monkeypatch.setattr(cadc_datatrail, "_MAX_SERVICE_WAIT", 0)
    monkeypatch.setattr(cadc_datatrail.time, "sleep", lambda *_: None)

    out_dir = tmp_path / "survey"
    rc = cli.main([
        "survey", "--telescope", "chime", "--source", "cadc-datatrail",
        "--scope", SCOPE, "--freq-ids", "614", "--out", str(out_dir),
    ])
    captured = capsys.readouterr()

    assert rc == 1
    assert "remained unreachable" in captured.err
    assert "Traceback" not in captured.err
    assert (out_dir / "inventory.jsonl").exists()
    assert (out_dir / "attempts.json").exists()
    assert not (out_dir / "inventory.meta.json").exists()


if __name__ == "__main__":
    rc = run_commit_decision_unit()
    rc = run_mixed_survey() or rc
    rc = run_empty_survey() or rc
    sys.exit(rc)
