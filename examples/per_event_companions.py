"""
Worked per-event analyzer with a companion side-load.

The runnable reference for two docs/ADDING_AN_ANALYZER.md sections:

  * "Per-event fan-out"  -- plan_runs returns one `{"events": [ev],
    "freq_ids": spec}` sub-selection per event, so each event becomes its own
    resumable product (`ev<event>[_<freq_ids>].npz`) consuming every selected
    freq_id of that event. Both sources understand the dict natively; against
    a local directory the event is parsed from filenames
    (--source-event-regex).

  * "Auxiliary inputs (gains, flags, companions)" -- the analyzer, not the
    engine, owns its per-event companion: the event -> companion lookup
    (companions.jsonl, e.g. from examples/match_inventories.py) is loaded once
    in begin(), resolved per file off meta["event"], stamped into the product,
    and VALIDATED on resume -- if the table now assigns this event a different
    companion, folding new files in would silently mix calibrations, so the
    resume refuses instead.

Runs are planned FROM the companion table, gated on freshness: an event whose
companion is staler than --set max_gain_lag_days is skipped, visibly, before a
byte is staged. Planning from the table (rather than the inventory) also makes
the same analyzer work unchanged against archive and local sources.

The science here is a stand-in -- mean |x|^2 over the stream, standing where a
real analysis would apply the companion (e.g. beamform baseband with a gain
solution). Every datatrawl integration point around it is real, and
tests/test_per_event_scan.py runs this file end to end through the CLI.

Try it on synthetic local files (see the test for a complete recipe):

    datatrawl scan --telescope chime --source local --reader chime-baseband \
        --plugin examples/per_event_companions.py --analyzer per-event-demo \
        --source-root /path/to/files --select 614,706 \
        --set companions=companions.jsonl --set max_gain_lag_days=30

`--select` stays the freq_id restriction: a per-event analyzer plans events
itself, so an event-shaped --select is rejected with a pointer, not misread.
"""
from __future__ import annotations

import json
import os

import numpy as np

from datatrawl.analyzer_base import AccumulatingAnalyzer
from datatrawl.interfaces import PluginInfo, READY, RunContext
from datatrawl.registry import analyzer as register_analyzer


def _load_companions(ctx: RunContext) -> dict:
    """event -> companion row, from --set companions=<path>."""
    path = (ctx.options or {}).get("companions")
    if not path or not os.path.exists(path):
        raise SystemExit(
            "per-event-demo needs the event->companion lookup: pass "
            "--set companions=/path/to/companions.jsonl "
            "(examples/match_inventories.py builds one from two inventories)")
    out = {}
    with open(path) as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                out[str(r["event"])] = r
    return out


@register_analyzer
class PerEventCompanionDemo(AccumulatingAnalyzer):
    info = PluginInfo(
        name="per-event-demo", kind="analyzer",
        summary="Worked example: one product per event, with a side-loaded "
                "per-event companion (stamped + resume-validated).",
        status=READY, instruments=("*",),
    )

    def __init__(self) -> None:
        super().__init__()
        self._event = None
        self._companion_name = None
        self._companion_lag = None
        self._power_sum = 0.0
        self._n_frames = 0

    # -- planning: one run per fresh-companion event --------------------------
    def plan_runs(self, ctx: RunContext, spec):
        if isinstance(spec, str) and spec.strip().lower().startswith(
                ("event:", "events:")):
            raise SystemExit(
                "per-event-demo plans one run per event itself; --select "
                "restricts freq_ids (e.g. --select 614,706). To process a "
                "subset of events, filter companions.jsonl.")
        max_lag = float((ctx.options or {}).get("max_gain_lag_days", 30))
        kept = []
        for ev, c in sorted(_load_companions(ctx).items()):
            lag = float(c.get("lag_days", -1))
            if 0 <= lag <= max_lag:
                kept.append(ev)
            else:                                    # visible, never silent
                print(f"[per-event-demo] skipping event {ev}: companion lag "
                      f"{lag:g}d outside 0..{max_lag:g}d")
        # An event in the table but absent from the data simply enumerates
        # zero units; the engine reports it and moves on.
        return [{"events": [ev], "freq_ids": spec} for ev in kept]

    # -- per-run lifecycle -----------------------------------------------------
    def begin(self, ctx: RunContext, first_meta) -> None:
        if self._event is None:                      # fresh run (not resumed)
            self._event = str(first_meta["event"])
            c = _load_companions(ctx)[self._event]
            self._companion_name = str(c["companion"]["name"])
            self._companion_lag = float(c.get("lag_days", -1))
            # A real analysis stages + reads the companion here, from
            # c["companion"]["common_path"] (cadcget) or a pre-staged path.

    def consume_file(self, arrays, meta) -> None:
        ev = str(meta["event"])
        if ev != self._event:                        # per-event fan-out invariant
            raise RuntimeError(f"unit from event {ev} in a {self._event} run")
        for frame in arrays:
            self._power_sum += float(np.mean(np.abs(frame) ** 2))
            self._n_frames += 1
        self._record(meta)

    # -- product + companion-aware resume --------------------------------------
    def _product(self):
        return {"analysis": "per-event-demo",
                "event": self._event,
                "companion_name": self._companion_name,
                "companion_lag_days": self._companion_lag,
                "mean_power": (self._power_sum / self._n_frames
                               if self._n_frames else 0.0),
                "power_sum": self._power_sum,
                "n_frames": self._n_frames}

    def _restore(self, z) -> None:
        self._event = str(z["event"])
        self._companion_name = str(z["companion_name"])
        self._companion_lag = float(z["companion_lag_days"])
        self._power_sum = float(z["power_sum"])
        self._n_frames = int(z["n_frames"])

    def resume(self, path: str, ctx: RunContext) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        if str(z.get("analysis")) != "per-event-demo":
            raise SystemExit(f"{path} was written by a different analysis")
        # The side-loaded companion is a resume parameter: if the table now
        # assigns this event a DIFFERENT companion, new files would be folded
        # in under one calibration and old ones under another -- silent
        # corruption. Refuse, with both names.
        current = _load_companions(ctx).get(str(z["event"]), {})
        now = str(current.get("companion", {}).get("name"))
        if now != str(z["companion_name"]):
            raise SystemExit(
                f"resume refused: {path} was built with companion "
                f"{z['companion_name']} but companions.jsonl now assigns "
                f"{now}. Rebuild the product (delete it) or restore the "
                f"original companion table.")
        self._restore(z)
        self._keys = [str(x) for x in z["unit_keys"]]
        self._names = [str(x) for x in z["files"]]
        return True
