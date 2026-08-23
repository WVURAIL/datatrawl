"""
Match a primary inventory to a companion inventory by event.

This is the offline join used by the auxiliary-input pattern in
docs/ADDING_AN_ANALYZER.md. `datatrawl` does not choose a gain, flag set, or N2
product for the analysis because that choice is part of the science. This
example makes one specific choice:

    For each primary event, use the latest companion date that is not later
    than the event date. If none exists, record the earliest companion and a
    negative lag.

The negative-lag fallback keeps the event visible instead of silently dropping
it. The worked per-event analyzer rejects negative lags by default, so it will
not use that future-dated companion unless its policy is changed.

There are three assumptions to check before using this for calibration:

  * `obs_date` has day resolution because survey derives it from the archive
    common path. Add a real timestamp in `survey_files` or `annotate_row` if the
    policy needs time-of-day resolution.
  * A companion from the same day counts as preceding.
  * If several companion rows have the same date, the last one in inventory
    order wins. Replace that tie rule if those rows are scientifically distinct.

Both inputs are JSONL inventories written for their respective products. The
output, `companions.jsonl`, contains one row per dated primary event:

    {"event": ..., "companion": {"name": ..., "common_path": ...,
                                 "obs_date": ...}, "lag_days": ...}

An analyzer can load that table once in `begin()` and look up each unit by
`meta["event"]`.

Run:
    python examples/match_inventories.py \
        --primary  data/chime/inventory.jsonl \
        --companion data/chime-gains/inventory.jsonl \
        --out companions.jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json


def _rows(path):
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _day(row):
    try:
        return dt.date.fromisoformat(str(row.get("obs_date", ""))[:10])
    except ValueError:
        return None    # Keep an unknown date visible in the final skipped count.


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--primary", required=True,
                    help="inventory.jsonl whose events need companions")
    ap.add_argument("--companion", required=True,
                    help="inventory.jsonl of the companion product")
    ap.add_argument("--out", default="companions.jsonl")
    args = ap.parse_args()

    companions = sorted(
        ((d, r) for r in _rows(args.companion) if (d := _day(r)) is not None),
        key=lambda t: t[0])
    if not companions:
        raise SystemExit(f"no dated rows in {args.companion} -- nothing to match")

    # A baseband inventory has many rows per event. The match is per event, not
    # per freq_id. setdefault deliberately keeps the first date; inventory rows
    # for one event are expected to agree on obs_date.
    events: dict = {}
    for r in _rows(args.primary):
        events.setdefault(str(r["event"]), _day(r))

    n_undated = sum(1 for d in events.values() if d is None)
    with open(args.out, "w") as out:
        for ev, day in sorted(events.items()):
            if day is None:
                continue
            # This loop is the matching policy: nearest preceding day, with the
            # earliest known companion retained as a visible future fallback.
            chosen = companions[0][1]
            lag = (day - companions[0][0]).days
            for cd, cr in companions:
                if cd > day:
                    break
                chosen, lag = cr, (day - cd).days
            out.write(json.dumps({
                "event": ev,
                "companion": {"name": chosen.get("name"),
                              "common_path": chosen.get("common_path"),
                              "obs_date": chosen.get("obs_date")},
                "lag_days": lag,
            }) + "\n")

    print(f"matched {len(events) - n_undated}/{len(events)} events "
          f"-> {args.out}"
          + (f" ({n_undated} undated event(s) skipped)" if n_undated else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
