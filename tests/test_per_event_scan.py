#!/usr/bin/env python3
"""
Pins the COMPOSITION the per-event/companion mechanisms exist for -- the path a
walkthrough of the first external use case exercised by hand. The unit pieces
(selection grammar, event filters, reader shape) are pinned in
test_event_selection_and_shape.py; this file pins them working together,
through the real CLI, with the shipped worked example as the analyzer:

    plan_runs from a companion table (stale event skipped, visibly)
      -> one dict sub-selection per event
      -> event-filtered enumerate (local source, filename-parsed events)
      -> per-event products named ev<event>_<freq_ids>.npz,
         companion identity stamped in
      -> rerun is a clean no-op resume
      -> a REASSIGNED companion refuses the resume (SystemExit, both names)

plus the offline join example (match_inventories.py) that builds the table,
run against the real GBO gain-acquisition dates that motivated all of this.

Everything is local + synthetic; no archive access, no fakes -- the local
source's event parsing is exactly what makes this composition testable
offline.

Run:  PYTHONPATH=src python -m pytest tests/test_per_event_scan.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys

import numpy as np
import pytest

import datatrawl.cli as cli
from datatrawl.plugins.readers import _baseband_format as fmt

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "..", "examples", "per_event_companions.py")
JOIN = os.path.join(HERE, "..", "examples", "match_inventories.py")

EV_OK1, EV_OK2, EV_STALE = "310070001", "320091502", "330112003"
GAIN_OK1 = "gain_20230629T200929Z.h5"      # the real GBO acquisition tags
GAIN_OK2 = "gain_20230909T224157Z.h5"
GAIN_STALE = "gain_20231009T162555Z.h5"
FREQ_IDS = (614, 706)


def _write_companions(path, ev1_gain=GAIN_OK1):
    rows = [(EV_OK1, ev1_gain, 2), (EV_OK2, GAIN_OK2, 6),
            (EV_STALE, GAIN_STALE, 42)]
    with open(path, "w") as f:
        for ev, gain, lag in rows:
            f.write(json.dumps({"event": ev, "lag_days": lag,
                                "companion": {"name": gain}}) + "\n")


@pytest.fixture()
def workspace(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for ev in (EV_OK1, EV_OK2, EV_STALE):
        for ch in FREQ_IDS:
            fmt.make_synth_file(str(src / f"baseband_{ev}_{ch}.h5"),
                                n_time=512, n_feeds=8,
                                f_center_mhz=600.0, f_tone_bb=1e3, seed=ch)
    comp = tmp_path / "companions.jsonl"
    _write_companions(str(comp))
    return tmp_path, str(src), str(comp)


def _scan(root, src_root, companions):
    argv = ["scan", "--telescope", "chime", "--source", "local",
            "--reader", "chime-baseband", "--analyzer", "per-event-demo",
            "--plugin", DEMO, "--source-root", src_root,
            "--select", "614,706", "--root", str(root),
            "--nfft", "128", "--max-frames-per-file", "2",
            "--set", f"companions={companions}",
            "--set", "max_gain_lag_days=30"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def _product(root, ev):
    return os.path.join(str(root), "results", "chime", "per-event-demo",
                        f"ev{ev}_614_706.npz")


def test_per_event_scan_end_to_end(workspace):
    root, src_root, comp = workspace
    rc, out = _scan(root, src_root, comp)
    assert rc == 0
    # the done line carries the example's summary (event + companion), not {}
    assert f"'event': '{EV_OK1}'" in out and GAIN_OK1 in out
    # the stale event is skipped BEFORE any staging, and says so
    assert f"skipping event {EV_STALE}" in out
    assert not os.path.exists(_product(root, EV_STALE))
    # one product per fresh event, companion identity stamped in
    for ev, gain in ((EV_OK1, GAIN_OK1), (EV_OK2, GAIN_OK2)):
        z = np.load(_product(root, ev), allow_pickle=False)
        assert str(z["event"]) == ev
        assert str(z["companion_name"]) == gain
        assert len(z["files"]) == len(FREQ_IDS)      # both freq_ids, this event only
        assert all(ev in str(n) for n in z["files"])
        assert int(z["n_frames"]) == 2 * len(FREQ_IDS)   # --max-frames-per-file 2


def test_rerun_is_a_noop_resume(workspace):
    root, src_root, comp = workspace
    _scan(root, src_root, comp)
    before = {ev: os.path.getmtime(_product(root, ev)) for ev in (EV_OK1, EV_OK2)}
    rc, out = _scan(root, src_root, comp)
    assert rc == 0
    assert out.count("selection already complete") == 2
    after = {ev: os.path.getmtime(_product(root, ev)) for ev in (EV_OK1, EV_OK2)}
    assert after == before                            # nothing rewritten


def test_reassigned_companion_refuses_resume(workspace):
    root, src_root, comp = workspace
    _scan(root, src_root, comp)
    _write_companions(comp, ev1_gain="gain_20230628T132124Z.h5")   # reassign
    with pytest.raises(SystemExit) as ei:
        _scan(root, src_root, comp)
    msg = str(ei.value)
    assert "resume refused" in msg
    assert GAIN_OK1 in msg and "gain_20230628T132124Z.h5" in msg   # both names


def test_match_inventories_join(tmp_path, monkeypatch):
    """The offline join example, against the real gain-acquisition dates:
    nearest-preceding by day, with the post-window (stale) lag visible."""
    prim, comp, out = (str(tmp_path / n) for n in
                       ("prim.jsonl", "gains.jsonl", "companions.jsonl"))
    with open(prim, "w") as f:
        for ev, d in ((EV_OK1, "2023-07-01"), (EV_OK2, "2023-09-15"),
                      (EV_STALE, "2023-11-20")):
            f.write(json.dumps({"event": ev, "freq_id": 614, "obs_date": d,
                                "common_path": "cadc:X/a"}) + "\n")
    with open(comp, "w") as f:
        for tag in ("20230629T200929Z", "20230909T224157Z", "20231009T162555Z"):
            f.write(json.dumps({"event": tag, "name": f"gain_{tag}.h5",
                                "obs_date": f"{tag[:4]}-{tag[4:6]}-{tag[6:8]}",
                                "common_path": "cadc:G/x"}) + "\n")
    monkeypatch.setattr(sys, "argv", ["match_inventories.py", "--primary", prim,
                                      "--companion", comp, "--out", out])
    with pytest.raises(SystemExit) as ei:
        runpy.run_path(JOIN, run_name="__main__")
    assert ei.value.code == 0
    rows = {r["event"]: r for r in map(json.loads, open(out))}
    assert rows[EV_OK1]["companion"]["name"] == GAIN_OK1
    assert rows[EV_OK1]["lag_days"] == 2
    assert rows[EV_OK2]["lag_days"] == 6
    assert rows[EV_STALE]["companion"]["name"] == GAIN_STALE
    assert rows[EV_STALE]["lag_days"] == 42


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
