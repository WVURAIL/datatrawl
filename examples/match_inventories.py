"""
Worked example: match companion products (gains, N2, flags) to a primary
inventory, by event -- the offline join step of the auxiliary-inputs pattern
in docs/ADDING_AN_ANALYZER.md.

datatrawl deliberately does not do this join for you (README: "Scope and
non-goals"). WHICH companion corresponds to an event -- nearest in time?
covering interval? maximum staleness? -- is science policy, and this file is
where that policy lives, in YOUR copy, under your control. The policy coded
below is the simplest defensible default:

    for each event in the primary inventory, pick the companion row with the
    latest obs_date <= the event's obs_date (nearest-preceding by day),
    falling back to the overall earliest companion if none precedes.

Two honest caveats before you trust it for calibration:

  * `obs_date` is DAY-granularity (survey parses it from the archive common
    path). If your matching needs finer than a day, put a real timestamp
    column in the companion rows via your shape reader's `survey_files`
    fields / `annotate_row`, and key on that here instead.
  * Same-day is treated as "preceding". Decide whether that is right for your
    product.

Inputs are the inventories survey wrote (one per product -- see
docs/ADDING_A_READER.md for surveying a non-baseband product). Output is
companions.jsonl: one row per primary event,
    {"event": ..., "companion": {"name": ..., "common_path": ...,
                                 "obs_date": ...}, "lag_days": ...}
which an analyzer loads once in begin() and resolves per unit off
meta["event"].

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
        return None    # "unknown" -> unmatchable; counted and reported


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

    # one row per primary EVENT (a baseband inventory has many rows per event;
    # the companion choice is per event, not per freq_id)
    events: dict = {}
    for r in _rows(args.primary):
        events.setdefault(str(r["event"]), _day(r))

    n_undated = sum(1 for d in events.values() if d is None)
    with open(args.out, "w") as out:
        for ev, day in sorted(events.items()):
            if day is None:
                continue
            # nearest-preceding by day; earliest companion as the fallback.
            # <-- THE POLICY. Edit here.
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
