"""
Demonstrate one product per event with a side-loaded companion table.

This file is the runnable reference for two sections of
docs/ADDING_AN_ANALYZER.md:

  * `plan_runs` returns one `{"events": [ev], "freq_ids": spec}` selection per
    event. Each selection receives a fresh analyzer and a product named
    `ev<event>[_<freq_ids>].npz` by default. The CADC and local sources both
    understand this selection shape; the local source parses event IDs from
    filenames with `--source-event-regex`.

  * The analyzer owns the companion lookup. It loads `companions.jsonl`, looks
    up units through `meta["event"]`, stores the chosen companion name in the
    product, and compares that name on resume. If the table later assigns a
    different name, the resume is refused rather than combining two named
    companions in one product.

Runs are planned from the companion table and filtered by
`--set max_gain_lag_days`. An event outside the accepted lag range is reported
before any data for that event is staged. Planning from the table also avoids
requiring an archive inventory merely to discover event IDs, so the same
analyzer can run against archive or local sources.

The companion *name* is the resume identity in this example. A real analysis
should stamp every field that can change the result, such as a checksum,
calibration version, or full URI. It should also decide how a changed lag value
affects resume compatibility.

For a day-keyed companion archive, the analyzer can replace the join table with
a lazy `DATATRAIL.files()` lookup in `begin()`; see "Day-keyed archives" in
docs/ADDING_AN_ANALYZER.md.

The statistic here is only a stand-in: mean |x|^2 over the streamed frames. It
does not read or apply the companion data. The fan-out, lookup, product, and
resume paths are exercised end to end by tests/test_per_event_scan.py.

Try it on synthetic local files (see the test for a complete recipe):

    datatrawl scan --telescope chime --source local --reader chime-baseband \
        --plugin examples/per_event_companions.py --analyzer per-event-demo \
        --source-root /path/to/files --select 614,706 \
        --set companions=companions.jsonl --set max_gain_lag_days=30

`--select` remains the freq_id restriction because the analyzer obtains its
event list from the companion table. An event-shaped `--select` is rejected
with an explanation.
"""
from __future__ import annotations

import json
import os

import numpy as np

from datatrawl.analyzer_base import AccumulatingAnalyzer
from datatrawl.interfaces import PluginInfo, READY, RunContext
from datatrawl.registry import analyzer as register_analyzer


def _load_companions(ctx: RunContext) -> dict:
    """Load event -> row from --set companions=<path>; later duplicates win."""
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

    # -- planning: one run per event that passes the lag policy ----------------
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
            else:                                    # Report every policy exclusion.
                print(f"[per-event-demo] skipping event {ev}: companion lag "
                      f"{lag:g}d outside 0..{max_lag:g}d")
        # A table event absent from the selected source enumerates zero units;
        # the CLI reports that product selection and moves on.
        return [{"events": [ev], "freq_ids": spec} for ev in kept]

    # -- per-run lifecycle -----------------------------------------------------
    def begin(self, ctx: RunContext, first_meta) -> None:
        if self._event is None:                      # fresh run (not resumed)
            self._event = str(first_meta["event"])
            c = _load_companions(ctx)[self._event]
            self._companion_name = str(c["companion"]["name"])
            self._companion_lag = float(c.get("lag_days", -1))
            # A real analysis would stage and read the companion here, using
            # c["companion"]["common_path"] or a pre-staged path.

    def consume_file(self, arrays, meta) -> None:
        ev = str(meta["event"])
        if ev != self._event:                        # Enforce one event per product.
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

    def summary(self):
        # This mapping appears on the engine's per-product completion line.
        return {"event": self._event, "companion": self._companion_name,
                "lag_days": self._companion_lag, "files": len(self._names),
                "mean_power": round(self._product()["mean_power"], 4)}

    def resume(self, path: str, ctx: RunContext) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        if str(z.get("analysis")) != "per-event-demo":
            raise SystemExit(f"{path} was written by a different analysis")
        # The selected companion name is a resume parameter in this example.
        # Refuse a changed assignment and report both names.
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
