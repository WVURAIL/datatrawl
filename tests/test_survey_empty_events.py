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
  * an empty event with a RECENT (or undatable) observation is NOT marked done
    on the first pass -- it is retried across resumes, so a rerun picks it up;
  * after _MAX_ATTEMPTS it is accepted-as-empty: marked done AND recorded in
    no_files_events.jsonl (visible, never silent, never re-probed forever);
  * an empty event whose observation is at least _EMPTY_TERMINAL_AGE_DAYS old
    is accepted-as-empty on FIRST sighting (absence is definitive per probe;
    the only transient it could mask is replication lag, which cannot affect
    an old observation), with a full ledger entry giving the reason;
  * a run whose inventory ends at 0 rows prints a loud [warn]; and
  * the happy path (freq_ids present -> rows written, event done) still works.

Run:  PYTHONPATH=src python tests/test_survey_empty_events.py
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile

import datatrawl.cli as cli
from datatrawl.plugins.sources import cadc_datatrail
from datatrawl.plugins.readers.chime_baseband import MINIMUM_ARCHIVE_BYTES
from datatrawl.interfaces import RunContext

SCOPE = "chime.event.baseband.raw"
ABSENT_EVENT = "100000001"          # cp resolves, every freq_id NotFound
PRESENT_EVENT = "900000009"         # cp resolves, every freq_id present + big
FREQ_IDS = [614, 706]
ABOVE_FLOOR = MINIMUM_ARCHIVE_BYTES + 1

# Common-Path date directories exercising the age gate's three regimes.
# RECENT sits well inside _EMPTY_TERMINAL_AGE_DAYS, so absence could still be
# replication lag -> retry lifecycle (the original pinned behavior). OLD is far
# past the threshold -> first-sight acceptance. UNDATED defeats _DATE_RE ->
# obs_date "unknown" -> the gate must fail open into the retry path.
RECENT_OBS_DIR = (datetime.date.today()
                  - datetime.timedelta(days=3)).strftime("raw/%Y/%m/%d")
OLD_OBS_DIR = "raw/2020/01/01"
UNDATED_OBS_DIR = "misc"


@contextlib.contextmanager
def fake_archive(membership, present_events, obs_dir=RECENT_OBS_DIR):
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
        lambda scope, ev, **kwargs:
        (f"cadc:CHIMEFRB/data/{obs_dir}/{ev}", True))
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
        "no_files": _rows(os.path.join(out_dir, "no_files_events.jsonl")),
    }


def _no_files_keys(st):
    """scope|event keys of the accept-as-empty ledger entries."""
    return [f"{r['scope']}|{r['event']}" for r in st["no_files"]]


def test_completeness_pending_uses_event_identity_not_only_counts(
        tmp_path, capsys):
    """A new pending event is not hidden by one stale completed DB row."""
    out = tmp_path / "inventory"
    ctx = RunContext(
        instrument=None, selection=None,
        options={"freq_ids": list(FREQ_IDS)},
    )
    with fake_archive(
            {(SCOPE, PRESENT_EVENT): ["dataset"]}, {PRESENT_EVENT}):
        complete = cadc_datatrail.CadcDatatrailSource()
        complete.survey(ctx, str(out))
    assert complete.survey_completeness_issues(str(out))["pending"] == 0

    # The enumeration changes but has the same cardinality. The new event is a
    # recent accepted-for-retry empty event, while the old completed key remains
    # in SQLite. A len(events)-len(surveyed) calculation would incorrectly say 0.
    with fake_archive(
            {(SCOPE, ABSENT_EVENT): ["dataset"]}, set(),
            obs_dir=RECENT_OBS_DIR):
        pending = cadc_datatrail.CadcDatatrailSource()
        pending.survey(ctx, str(out))
    assert pending.survey_completeness_issues(str(out)) == {
        "incomplete": 0, "refused": 0, "pending": 1,
    }
    capsys.readouterr()


# --------------------------------------------------------------------------
# 1) pure decision function
# --------------------------------------------------------------------------
def run_commit_decision_unit() -> int:
    cd = cadc_datatrail._commit_decision
    M = cadc_datatrail._MAX_ATTEMPTS
    # (label, got, want) for (write_records, mark_done, incomplete)
    cases = [
        ("clean (rows)",      cd(0, 0, 4),     (True, True, False)),
        ("partial retry",     cd(2, 0, 2),     (False, False, False)),
        ("partial accept",    cd(2, M - 1, 2), (True, True, True)),
        ("empty retry",       cd(0, 0, 0),     (False, False, False)),
        ("empty accept",      cd(0, M - 1, 0), (False, True, False)),
        # the age gate: empty_max_attempts=1 (old obs) accepts on first
        # sighting; None leaves the young/undatable retry lifecycle untouched;
        # and the errored/partial path must ignore the empty-only cap.
        ("empty aged-out first sight",
         cd(0, 0, 0, empty_max_attempts=1),
         (False, True, False)),
        ("empty young unaffected",
         cd(0, 0, 0, empty_max_attempts=None),
         (False, False, False)),
        ("partial ignores empty cap",
         cd(2, 0, 2, empty_max_attempts=1),
         (False, False, False)),
    ]
    ok = True
    for label, got, want in cases:
        if got != want:
            print(f"  FAIL: _commit_decision {label}: got {got}, want {want}")
            ok = False
    # the crux: empty must NOT mark done while attempts remain, and clean must.
    if cd(0, 0, 0)[1] is not False:
        print("  FAIL: empty-with-attempts-left was marked done"); ok = False
    if cd(0, 0, 4)[1] is not True:
        print("  FAIL: clean event was not marked done"); ok = False
    print("  _commit_decision: clean / partial / empty verdicts all correct")
    print("COMMIT-DECISION UNIT PASSED" if ok else "COMMIT-DECISION UNIT FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 2) survey() end-to-end: empty event coexists with a real one
# --------------------------------------------------------------------------
def run_mixed_survey() -> int:
    print("MIXED SURVEY (one present event, one all-absent RECENT event)")
    work = tempfile.mkdtemp(prefix="dtw_mixed_")
    ok = True
    try:
        membership = {
            (SCOPE, ABSENT_EVENT): ["dataset_a"],
            (SCOPE, PRESENT_EVENT): ["dataset_a"],
        }
        out_dir = os.path.join(work, "data", "chime-test")
        # RECENT obs (the fake_archive default): the age gate must NOT fire,
        # preserving the original retried-across-resumes contract.
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
        if absent_key not in _no_files_keys(st):
            print("  FAIL: accepted-empty event not recorded in "
                  "no_files_events.jsonl")
            ok = False
        elif st["no_files"][0].get("reason") != "max-attempts":
            print(f"  FAIL: recent-obs acceptance should be reason="
                  f"max-attempts, got {st['no_files'][0].get('reason')!r}")
            ok = False
        elif st["no_files"][0].get("attempts") != M:
            print(f"  FAIL: ledger should record {M} sightings, got "
                  f"{st['no_files'][0].get('attempts')!r}")
            ok = False
        if st["rows"]:
            print("  FAIL: rows appeared for an all-absent event"); ok = False
        if "accepting as empty" not in log_last:
            print("  FAIL: acceptance was not announced in the log"); ok = False
        print(f"  run 1: 0 rows + [warn] + not-done; after {M} runs: "
              f"accepted-empty, done, in the ledger (reason=max-attempts)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("EMPTY SURVEY PASSED" if ok else "EMPTY SURVEY FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 4) survey() end-to-end: an OLD absent event is accepted on first sighting
# --------------------------------------------------------------------------
def run_aged_empty_survey() -> int:
    print("AGED EMPTY SURVEY (old obs; accept-as-empty on FIRST sighting)")
    work = tempfile.mkdtemp(prefix="dtw_aged_")
    ok = True
    try:
        membership = {(SCOPE, ABSENT_EVENT): ["dataset_a"]}
        out_dir = os.path.join(work, "data", "chime-test")
        absent_key = f"{SCOPE}|{ABSENT_EVENT}"
        with fake_archive(membership, present_events=[],
                          obs_dir=OLD_OBS_DIR):
            log1 = _survey(out_dir)
            st1 = _state(out_dir)
            log2 = _survey(out_dir)        # must skip, not re-probe

        if absent_key not in st1["surveyed"]:
            print("  FAIL: aged empty event not marked done on first sighting")
            ok = False
        if "accepting as empty" not in log1:
            print("  FAIL: first-run acceptance not announced in the log")
            ok = False
        if "inventory.jsonl is EMPTY" not in log1:
            print("  FAIL: 0-row run lost its [warn]"); ok = False
        if len(st1["no_files"]) != 1:
            print(f"  FAIL: expected 1 ledger entry, got "
                  f"{len(st1['no_files'])}"); ok = False
        else:
            e = st1["no_files"][0]
            want = {"scope": SCOPE, "event": ABSENT_EVENT, "attempts": 1,
                    "n_expected": len(FREQ_IDS), "obs_date": "2020-01-01",
                    "reason": "aged-out"}
            bad = {k: (e.get(k), v) for k, v in want.items() if e.get(k) != v}
            if bad:
                print(f"  FAIL: ledger fields wrong (got, want): {bad}")
                ok = False
            if (not e.get("ts") or not e.get("common_path")
                    or (e.get("age_days") or 0)
                    < cadc_datatrail._EMPTY_TERMINAL_AGE_DAYS):
                print(f"  FAIL: ledger missing ts/common_path or age too "
                      f"young: {e}"); ok = False
        if "survey: 0 events this run" not in log2:
            print("  FAIL: accepted event was re-probed on the next run")
            ok = False
        print("  first sighting -> accepted-empty + done + full ledger entry "
              "(reason=aged-out); second run skips it")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("AGED EMPTY SURVEY PASSED" if ok else "AGED EMPTY SURVEY FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 5) survey() end-to-end: an UNDATABLE absent event must keep the retry path
# --------------------------------------------------------------------------
def run_undated_empty_survey() -> int:
    print("UNDATED EMPTY SURVEY (no obs date in the path; gate fails open)")
    work = tempfile.mkdtemp(prefix="dtw_undated_")
    ok = True
    try:
        membership = {(SCOPE, ABSENT_EVENT): ["dataset_a"]}
        out_dir = os.path.join(work, "data", "chime-test")
        absent_key = f"{SCOPE}|{ABSENT_EVENT}"
        with fake_archive(membership, present_events=[],
                          obs_dir=UNDATED_OBS_DIR):
            log1 = _survey(out_dir)
            st1 = _state(out_dir)

        if absent_key in st1["surveyed"]:
            print("  FAIL: undatable empty event was accepted on first "
                  "sighting (the gate must fail open)"); ok = False
        if st1["no_files"]:
            print("  FAIL: undatable empty event entered the ledger early")
            ok = False
        if "re-checking in case transient" not in log1:
            print("  FAIL: undatable empty event did not take the retry path")
            ok = False
        print("  undatable obs -> NOT accepted on first sighting; retry "
              "lifecycle preserved")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("UNDATED EMPTY SURVEY PASSED" if ok
          else "UNDATED EMPTY SURVEY FAILED")
    return 0 if ok else 1


# -- pytest entry points ---------------------------------------------------
def test_commit_decision_unit():
    assert run_commit_decision_unit() == 0


def test_mixed_survey():
    assert run_mixed_survey() == 0


def test_empty_survey():
    assert run_empty_survey() == 0


def test_aged_empty_survey():
    assert run_aged_empty_survey() == 0


def test_undated_empty_survey():
    assert run_undated_empty_survey() == 0


def test_sustained_service_outage_is_nonzero_and_preserves_state(
        monkeypatch, tmp_path, capsys):
    membership = {(SCOPE, ABSENT_EVENT): ["dataset_a"]}
    monkeypatch.setattr(
        cadc_datatrail, "_enumerate_events", lambda *a, **k: dict(membership)
    )
    monkeypatch.setattr(
        cadc_datatrail.DATATRAIL, "common_path",
        lambda scope, event, **kwargs: (None, False),
    )
    monkeypatch.setattr(cadc_datatrail, "_MAX_SERVICE_WAIT", 0)
    monkeypatch.setattr(cadc_datatrail.time, "sleep", lambda *_: None)
    # This test isolates the post-preflight outage path. The command boundary's
    # prerequisite behavior has its own focused tests in test_cli_execution.py.
    monkeypatch.setattr(
        cadc_datatrail.CadcDatatrailSource, "preflight",
        lambda self, ctx: (True, []),
    )
    monkeypatch.setattr(
        cadc_datatrail.Datatrail, "installed", staticmethod(lambda: True),
    )

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
    rc = run_aged_empty_survey() or rc
    rc = run_undated_empty_survey() or rc
    sys.exit(rc)
