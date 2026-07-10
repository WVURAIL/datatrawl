# Adding an analyzer

An analyzer is the science plugin. It consumes arrays from a reader and writes a small,
resumable product.

The public and internal plugin type is `analyzer`.

## Minimal analyzer

```python
import numpy as np

from datatrawl.interfaces import PluginInfo, RunContext
from datatrawl.analyzer_base import AccumulatingAnalyzer
from datatrawl.registry import analyzer as register_analyzer


@register_analyzer
class MyAnalyzer(AccumulatingAnalyzer):
    info = PluginInfo(
        name="my-analyzer",
        kind="analyzer",
        summary="Accumulate mean power from streamed arrays.",
        instruments=("*",),
        produces="my-analyzer/<selection>.npz",
        requires=("numpy",),
    )

    def __init__(self):
        super().__init__()
        self._count = 0
        self._sum = 0.0

    def begin(self, ctx: RunContext, first_meta):
        # begin() is also called after resume() when new files remain.
        # Capture first-file metadata here if needed, but do not reset
        # accumulators restored by _restore().
        pass

    def consume_file(self, arrays, meta):
        n = 0

        for arr in arrays:
            # Readers may yield complex baseband arrays. Accumulate a real,
            # nonnegative statistic rather than casting a complex mean to float.
            self._sum += float(np.mean(np.abs(arr) ** 2))
            n += 1

        self._record(meta)
        self._count += n
        return n

    def _product(self):
        return {
            "count": self._count,
            "sum": self._sum,
        }

    def _restore(self, z):
        self._count = int(z["count"])
        self._sum = float(z["sum"])

    def summary(self):
        return {"frames": self._count}
```

## Contract

### Single pass

`consume_file(arrays, meta)` receives an iterator. The staged file is deleted after
`consume_file` returns.

### Bounded memory

Accumulate statistics or sparse detections. Do not save full baseband or processed data
unless that is explicitly the product.

### Record processed units

Call:

```python
self._record(meta)
```

once per successfully consumed file, or implement equivalent `processed_keys()` behavior.
This enables resume.

### Preserve the reader/analyzer failure boundary

A probe failure happens before the analyzer sees a file, so the engine can quarantine
that file and continue safely. If a reader fails while yielding arrays, the analyzer
may already hold partial in-memory updates. The engine therefore records the
quarantine and aborts without checkpointing; rerun the same command to resume from
the last clean checkpoint and skip the quarantined file.

An unexpected exception from `consume_file` is an analyzer failure, not evidence that
the input file is corrupt. The scan stops, the file is not quarantined, and the
current in-memory state is not checkpointed. Fix the analyzer and rerun.

If a data condition is expected, handle it explicitly in the reader or analyzer
rather than relying on an unhandled exception to skip the file.

### Declare order-dependence

By default the engine delivers files in source (enumerate) order, but a user can raise
`--download-workers` or `--max-staged-files` above 1 to overlap downloads, which delivers
files in completion order instead. If your product depends on consume order -- a CFAR
baseline, a running/trailing statistic, anything that is not a commutative accumulation --
set:

```python
class MyAnalyzer(AccumulatingAnalyzer):
    requires_in_order = True
```

The engine then refuses those parallel settings for your analyzer (with an actionable
error) rather than silently producing an order-dependent result. Leave it unset (the
default, `False`) for a commutative accumulation like a summed PSD, which is correct at any
worker count and can parallelise freely.

### Validate resume parameters

If an option changes product meaning, save it and refuse incompatible resumes.

Examples:

- `freq_id`;
- `nfft`;
- detector threshold;
- window;
- Nyquist zone;
- max frames per file;
- calibration constants.

**Do this validation in `resume()`, not `begin()`.** The engine calls
`resume(path, ctx)` whenever the product file exists, but it skips `begin()`
entirely when every unit is already in the product (a re-run of a finished
product is a no-op that never reads a file). A check placed only in `begin()`
therefore fails to guard an already-complete product: re-running it with a
different threshold would silently report success against the *old* product
instead of refusing. `resume()` receives `ctx`, so compare the loaded product's
saved parameters against `ctx.options` (or `ctx.instrument`) there and raise
`SystemExit` on a mismatch.

The minimal `AccumulatingAnalyzer` shape above uses `_restore(z)`, which has no
`ctx` and so cannot see the current run's options. To validate run parameters,
override `resume()` in full (reusing `self._atomic_savez()` for the crash-safe
write) -- `plugins/analyzers/spectrum.py` is the worked reference: it stamps
`freq_id`, `nfft`, `nyquist_zone`, and `max_frames_per_file` into the product
and rejects any resume that disagrees.

### Use fan-out only when products are independent

If one product should be produced per freq_id or per pilot:

```python
def plan_runs(self, ctx, spec):
    # One product per freq_id: return one single-freq_id sub-selection each.
    # Turn `spec` (the --select string) into freq_id ints however your
    # analysis defines them. The shipped spectrum analyzer's _parse_freq_ids()
    # in plugins/analyzers/spectrum.py is a worked parser for "844",
    # "614,706", and "506-844" that you can lift.
    freq_ids = [int(x) for x in str(spec).split(",")]   # minimal: a comma list
    return [[fid] for fid in freq_ids]
```

If one product should cover all input units, use the default one-run behavior.

### Per-event fan-out

The fan-out above is per-freq_id -- one product per channel, across all events.
An event-oriented analysis inverts that: one product per EVENT, consuming every
selected freq_id of that event (e.g. beamforming an FRB event's baseband into a
singlebeam and its SNR). Return one sub-selection dict per event; the sources
understand the `events` key natively:

```python
def plan_runs(self, ctx, spec):
    # One product per event. The archive inventory is the event list; its
    # path is on ctx.options (scan resolves --name/--inventory before runs
    # are planned). `spec` (--select) stays the freq_id restriction.
    import json
    events, seen = [], set()
    with open(ctx.options["inventory"]) as fh:
        for line in fh:
            ev = str(json.loads(line)["event"])
            if ev not in seen:
                seen.add(ev); events.append(ev)
    return [{"events": [ev], "freq_ids": spec} for ev in events]
```

Each run then enumerates exactly one event's files and writes its own
resumable product (named `ev<event>[_<freq_ids>].npz` by default). Files of one
event arrive per-freq_id in inventory order; set `requires_in_order = True` if
your combine depends on that order rather than accumulating commutatively.
Against a local directory the same selection works via the filename-parsed
event (`--source-event-regex`, default `baseband_(\d+)_`).

Two conventions worth keeping: `--select` stays the *freq_id* restriction for a
per-event analyzer -- it plans events itself, so reject an event-shaped
`--select` with a pointer rather than letting it be misread. And when runs are
gated on a companion (next section), plan from the companion table instead of
the inventory: only calibratable events get runs, and the same analyzer then
works unchanged against archive and local sources. The runnable reference for
both is [`examples/per_event_companions.py`](../examples/per_event_companions.py),
driven end to end through the CLI by `tests/test_per_event_scan.py`.

## Run parameters (`--set`)

Analyzer parameters travel through `ctx.options`. On the command line they are
set generically, so the CLI stays analysis-agnostic:

```bash
datatrawl scan ... --analyzer my-analyzer --set bracket_hz=400 --set window=hann
```

The mechanics:

- `--set key=value` is repeatable and exists on all four run-facing commands
  (`doctor`, `survey`, `explore`, `scan`). Sources read the same dict, which is
  how a custom source takes its own settings
  (see [`ADDING_A_SOURCE.md`](ADDING_A_SOURCE.md)).
- Values get best-effort typing before they reach the analyzer: `true`/`false`
  become bools, integers and floats are parsed, `none`/`null` drops the key,
  and anything else stays a string. Validate and coerce in the analyzer rather
  than assuming a type survived the command line.
- `ctx.options` is one shared namespace. The engine resolves its own keys into
  it first (`inventory`, `root`, `source_root`, `gpu`, ...), and `--set` pairs
  merge last. Pick distinctive parameter names and read them with
  `ctx.options.get("my_key", default)`.
- A `--set` parameter that changes the meaning of the product is a resume
  parameter: stamp it into the product and check it in `resume()` -- see
  [Validate resume parameters](#validate-resume-parameters) above.

## Auxiliary inputs (gains, flags, companions)

Some analyses need a small companion file per unit -- calibration gains to
beamform baseband, a flag table, an ephemeris. The engine will not stage these
for you, deliberately: a unit is one primary file, consumed independently and
deleted after analysis. The engine may prefetch other units within the
`--max-staged-files` bound, but it never presents them as a grouped input. That
unit boundary keys resume (see "Scope and non-goals" in the README). The
supported companion pattern is a side-load owned by the analyzer:

1. **Build the lookup offline.** Survey each companion product into its own
   inventory (a reader shape per product -- `docs/ADDING_A_READER.md`), then
   join them to your primary inventory with your matching policy.
   `examples/match_inventories.py` is a worked starting point that emits a
   `companions.jsonl` keyed by event.
2. **Load the lookup once, in `begin()`.** A dict of event -> companion path
   (pre-staged on /arc) or event -> CADC URI (fetched on demand) is small;
   hold it in memory.
3. **Resolve per file, in `consume_file()`.** Every unit's `meta` carries its
   `event` (archive and local sources both set it), so the lookup key is
   `meta["event"]`. If you fetch on demand, cache per event -- with per-event
   fan-out each run touches exactly one event, so that is one fetch per
   product -- and clean up what you staged; the engine only deletes what IT
   staged.
4. **Validate on resume.** A companion that changes the product's meaning
   (which gain solution was applied) is a resume parameter like any other:
   stamp its identity into the product and refuse a mismatched resume.

If the companion is not small -- if every unit requires another bulk input
alongside it -- you are fighting the one-primary-file-per-`Unit` model.
Pre-combine the products upstream or use a different engine rather than
enlarging the side-load.

**Day-keyed archives: resolve lazily instead.** Some companion products are
organized by DAY, not by event -- CHIME calibration gains, for instance, live
as one dataset per date with the files inside. There the offline join
dissolves: the recon map (`--scopes-only --match ... --expand --name gains`)
already lists exactly which days exist, and the analyzer resolves its
companion per event with one archive call:

```python
import json
from datatrawl.plugins.sources import DATATRAIL

days = sorted(json.loads(l)["dataset"]                 # scopes-gains.jsonl
              for l in open(ctx.options["gains_map"]))
day = max(d for d in days if d <= event_date)          # nearest present day
cp, names, ok = DATATRAIL.files("gbo.acquisition.processed", day)
if not ok:
    raise RuntimeError("datatrail did not answer -- retry, don't skip")
gain = pick(names)          # YOUR policy: calibrator, noise_weighted, ...
uri = f"{cp}/{gain}"        # fetch with cadcget; cache per event
```

`DATATRAIL.files()` is the programmatic `datatrail ps -s` -- use it (via
`datatrawl.plugins.sources`) rather than scraping the table or importing
dtcli internals; the adapter is the one sanctioned dtcli surface. ok=False
means the service did not answer, which is never the same as "no gain".
Which file on a day is THE companion is science policy, not archive
mechanics -- same rule as always.

Steps 2--4 are worked, runnable, and CLI-tested in
[`examples/per_event_companions.py`](../examples/per_event_companions.py):
companion loaded in `begin()`, resolved off `meta["event"]`, stamped into the
product, and a reassigned companion refusing the resume.

## Loading external analyzers

```bash
datatrawl scan --plugin /arc/project/my_analyzer.py --analyzer my-analyzer ...
```

or:

```bash
export DATATRAWL_PLUGINS=/arc/project/my_analyzer.py
datatrawl scan --analyzer my-analyzer ...
```

or package entry point:

```toml
[project.entry-points."datatrawl.plugins"]
my-analyzer = "my_project.datatrawl_plugins.my_analyzer"
```

Install or reinstall the package after changing its entry points so the metadata is
visible to `datatrawl`:

```bash
pip install -e /path/to/my_project
```

Loading a single file by path (`--plugin .../my_analyzer.py`) cannot resolve
package-relative imports (`from . import ...`): the file is loaded standalone, with
no parent package. If your plugin lives in a package (importing siblings or shared
helpers), make the package importable (`pip install -e .`, or put its root on
`PYTHONPATH`) and load it by module name (`--plugin my_project.my_analyzer`) or via
the entry point above.

The packaged form is available ready-made: the template repository
[`WVURAIL/datatrawl-analyzer-template`](https://github.com/WVURAIL/datatrawl-analyzer-template)
ships the src layout, the declared entry point, and a smoke suite that runs the
real engine on synthetic data. "Use this template", rename per its checklist,
`pip install -e .`, and the analyzer is discoverable with no `--plugin` flag.

## Preflight checklist

Before archive scale, keep the bounded smoke-test product separate from the full run:

```bash
PLUGIN=/path/to/my_analyzer.py   # or: my_project.datatrawl_plugins.my_analyzer

datatrawl list analyzers --plugin "$PLUGIN"
datatrawl doctor --plugin "$PLUGIN" \
  --telescope <instrument> --source <source> --reader <reader> --analyzer <name>
datatrawl scan --plugin "$PLUGIN" \
  --name <inventory> --analyzer <name> \
  --max-files 1 --max-frames-per-file 1 \
  --out smoke/my-analyzer.npz
```

Run the identical smoke command a second time. It should skip the completed unit
and avoid duplicating output. For the uncapped analysis, omit the bounds and use a
fresh output path; capped and uncapped products are intentionally incompatible.
