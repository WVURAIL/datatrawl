# Adding an analyzer

An analyzer contains the science operation. It consumes the arrays produced by a reader
and accumulates a small product that can be checkpointed and resumed. Built in or
external, the plugin kind is `analyzer`: the registry decorator and
`datatrawl list analyzers` both use this name.

The fastest start is the
[`WVURAIL/datatrawl-analyzer-template`](https://github.com/WVURAIL/datatrawl-analyzer-template)
repository; this guide documents the contract the template already implements.

## Minimal analyzer

```python
import numpy as np

from datatrawl.interfaces import (
    PluginInfo, RunContext, STREAM_COMPLEX_BASEBAND,
)
from datatrawl.analyzer_base import AccumulatingAnalyzer
from datatrawl.registry import analyzer as register_analyzer


@register_analyzer
class MyAnalyzer(AccumulatingAnalyzer):
    _PRODUCT_SCHEMA = "my-organization.my-analyzer/v1"

    info = PluginInfo(
        name="my-analyzer",
        kind="analyzer",
        summary="Accumulate mean power from streamed arrays.",
        instruments=("*",),
        produces="my-analyzer/<selection>.npz",
        requires=("numpy",),
        accepts_stream_kinds=(STREAM_COMPLEX_BASEBAND,),
    )

    def __init__(self):
        super().__init__()
        self._count = 0
        self._sum = 0.0
        self._power_scale = 1.0

    def resume_parameters(self, ctx: RunContext):
        # Normalize every analyzer option that changes product meaning.
        cap = ctx.options.get("max_frames_per_file")
        return {
            "power_scale": float(ctx.options.get("power_scale", 1.0)),
            "max_frames_per_file": int(cap) if cap is not None else None,
        }

    def begin(self, ctx: RunContext, first_meta):
        # begin() is also called after resume() when new files remain.
        # Capture first-file metadata here if needed, but do not reset
        # accumulators restored by _restore().
        params = self.resume_parameters(ctx)
        self._power_scale = params["power_scale"]

    def consume_file(self, arrays, meta):
        n = 0

        for arr in arrays:
            # Readers may yield complex baseband arrays. Accumulate a real,
            # nonnegative statistic rather than casting a complex mean to float.
            self._sum += float(np.mean(np.abs(arr) ** 2))
            n += 1

        if n:
            self._record(meta)
        self._count += n
        return n

    def _product(self):
        return {
            "count": self._count,
            "sum": self._sum,
            "power_scale": self._power_scale,
        }

    def _restore(self, z):
        self._count = int(z["count"])
        self._sum = float(z["sum"])
        self._power_scale = float(z["power_scale"])

    def summary(self):
        mean = (self._sum / self._count if self._count else 0.0)
        return {"frames": self._count,
                "mean_power": mean * self._power_scale}
```

## Contract

### Declare accepted reader streams

`accepts_stream_kinds` identifies the semantic reader output the analyzer can consume.
For `STREAM_COMPLEX_BASEBAND`, each item is a NumPy-compatible complex array with time on
axis 0; probe metadata includes `f_center_hz`, while the run instrument supplies sample
rate and Nyquist-zone geometry. List every supported kind explicitly. Use `STREAM_ANY`
only for an analyzer that genuinely validates and handles arbitrary stream objects.

`doctor` reports an explicit mismatch as NOT READY, and `scan` refuses it before inventory
enumeration or staging. Every analyzer must declare a non-empty
`accepts_stream_kinds`; registration/validation reports an omitted declaration as an
error. Add the real contract instead of using `STREAM_ANY` as a compatibility escape.

### Single pass

`consume_file(arrays, meta)` receives an iterator for one staged file. The engine deletes
that staged copy after `consume_file` returns, so the analyzer must finish all work on the
file during this call.

### Bounded memory

Accumulate statistics or sparse detections as the arrays arrive. Holding complete
baseband files or full processed arrays defeats the bounded streaming model and should be
done only when that bulk output is the intended product.

### Record processed units

After one file contributes at least one item successfully, call:

```python
self._record(meta)
```

Call `_record(meta)` once for that file, or implement equivalent `processed_keys()`
behavior. Do not record an all-empty/all-ragged file that contributed nothing; otherwise a
resume can skip input that the product does not represent. These processed keys tell the
engine which units the saved product already contains when a run resumes.

### Preserve the reader/analyzer failure boundary

A probe failure occurs before the analyzer receives the file. The engine can quarantine
that unit and continue because no analyzer state has changed.

A streaming reader failure has a different boundary. The analyzer may already contain
partial in-memory updates from that file. The engine records the quarantine and stops
without saving the current state. Rerunning the same command loads the last completed
checkpoint and skips the quarantined unit.

An unexpected exception from `consume_file` is treated as an analyzer failure, not as
evidence of a corrupt input. The scan stops, does not quarantine the file, and does not
checkpoint the current in-memory state. Fix the analyzer and rerun the command. Expected
data conditions should be handled explicitly in the reader or analyzer rather than with
an unhandled exception.

### Declare order-dependence

With the default staging settings, the engine delivers files in source enumeration order.
That ordering is not part of the public contract when either `--download-workers` or
`--max-staged-files` is above 1, and units may then arrive out of source order. Multiple
staging slots permit fetch/analyze overlap; concurrent fetches require multiple workers
and multiple slots. An analyzer that uses a CFAR baseline, a running or trailing
statistic, or any other non-commutative accumulation must declare that order requirement:

```python
class MyAnalyzer(AccumulatingAnalyzer):
    requires_in_order = True
```

The engine then refuses parallel settings for that analyzer. Leave
`requires_in_order` at its default value of `False` for a commutative accumulation such as
a summed PSD, because its result does not depend on file delivery order.

### Declare resume parameters

`AccumulatingAnalyzer` writes a canonical resume manifest alongside the product fields.
It validates that manifest before `_restore()` and refuses an existing product when its
analyzer identity, instrument geometry, selection, or declared parameters differ.
Products made by the old base format without this manifest are refused rather than
treated as compatible.

The current base-manifest topology is `datatrawl.accumulating/v3`; its version
changes when the manifest structure or interpretation changes. Analyzer authors
should version `_PRODUCT_SCHEMA` independently for their own persisted science
contract.

Set `_PRODUCT_SCHEMA` to a stable identity ending in `/vN`, as in the example.
It is written both as `_datatrawl_product_schema` and inside the manifest, and
resume checks it before restoring accumulators. Increment the version whenever
persisted fields or scientific/algorithm semantics change. Existing products
with a missing or different identity are deliberately refused; continue them
only with the exact code contract that created them, or choose a fresh output.
The base has a class-qualified `/v1` fallback so older external plugin source
continues to run, but production analyzers should declare their identity
explicitly so renaming a Python class does not become the versioning policy.

If an option changes the meaning of the product, return its normalized value from
`resume_parameters(ctx)`. Store it in the visible product fields as well when downstream
users need to inspect that provenance.

Examples:

- `freq_id`;
- `nfft`;
- detector threshold;
- window;
- Nyquist zone;
- max frames per file;
- calibration constants.

The base implementation calls `resume_parameters(ctx)` during both save and resume, so
the check also runs when every selected unit is already present and `begin()` is never
called. The default hook returns all of `ctx.options`: this is fail-closed, but may reject
a scientifically identical run merely because an operational path changed. Override the
hook with the smallest complete mapping of meaning-changing inputs, as in the minimal
example. Normalize equivalent forms (`1` versus `1.0`, sorted sets, resolved paths) before
returning them so compatibility is deterministic.

An analyzer with a custom file format may still override `save()` and `resume()`, but then
it owns equivalent identity, parameter, and atomic-write checks. The built-in spectrum
analyzer is the custom-format reference.

### Use fan-out only when products are independent

Use `plan_runs()` when one selection should produce several independent products. The
following pattern creates one product per `freq_id` (or any per-item split your
analysis defines):

```python
def plan_runs(self, ctx, spec):
    # One product per freq_id. Reuse the public, bounded grammar shared by
    # shipped sources and analyzers: "844", "614,706", or "506-844".
    from datatrawl.selection import parse_freq_ids
    freq_ids = parse_freq_ids(
        spec, n_channels=ctx.instrument.n_channels,
    )
    if freq_ids is None:
        raise SystemExit("this analyzer needs explicit freq_ids")
    return [[fid] for fid in sorted(freq_ids)]
```

If the product combines every selected input unit, keep the default single-run behavior.

### Per-event fan-out

The previous example creates one product per `freq_id` across all events. An
event-oriented analysis instead needs one product per event and consumes every selected
`freq_id` for that event. Beamforming an FRB event into a single beam and SNR is one such
case. Return one sub-selection dictionary per event; the shipped sources interpret the
`events` key directly.

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

Each sub-selection enumerates one event and writes an independent resumable product. The
default name is `ev<event>[_<freq_ids>].npz`. Files for that event arrive by `freq_id` in
inventory order when the default staging settings are used. Set
`requires_in_order = True` if the combination depends on that ordering. For a local
directory, the source obtains the event from the filename using `--source-event-regex`,
whose default is `baseband_(\d+)_`.

In this pattern, keep `--select` as the `freq_id` restriction and let `plan_runs()` choose
the events. Reject an event-shaped `--select` with a clear instruction so it cannot be
misread as a frequency selection.

If a companion file determines which events can be processed, plan the runs from the
companion table rather than the primary inventory. This creates products only for events
with a valid companion and keeps the analyzer behavior the same for archive and local
sources. [`examples/per_event_companions.py`](../examples/per_event_companions.py) is the
runnable reference, and `tests/test_per_event_scan.py` exercises the pattern through the
CLI.

## Run parameters (`--set`)

Analyzer parameters are passed through `ctx.options`. We use the generic `--set` option so
the CLI does not need analysis-specific flags.

```bash
datatrawl scan ... --analyzer my-analyzer --set bracket_hz=400 --set window=hann
```

The parameter path has these rules:

- `--set key=value` is repeatable on `doctor`, `survey`, `explore`, and `scan`. Sources use
  the same dictionary for their settings, as described in
  [`ADDING_A_SOURCE.md`](ADDING_A_SOURCE.md).
- The CLI applies best-effort typing. It converts `true` and `false` to Boolean values,
  parses integers and floats, converts `none` and `null` to an explicit `None`, and leaves
  other values as strings. The explicit null is preserved. Validate and normalize each
  value in the analyzer.
- Read plugin values directly from `ctx.options`, for example
  `ctx.options.get("my_key", default)`.
- Names reserved for core settings (such as `inventory`, `root`, `source_root`, and `gpu`)
  cannot be passed through `--set`. The CLI rejects the collision instead of allowing a
  plugin value to overwrite engine configuration. Use a distinctive plugin-specific key.
- A `--set` value that changes product meaning is also a resume parameter. Save it in the
  product and return it from `resume_parameters()` as described in
  [Declare resume parameters](#declare-resume-parameters).

## Auxiliary inputs (gains, flags, companions)

Some analyses need auxiliary information such as calibration gains, flags, or an
ephemeris. The engine stages one primary file for each `Unit` and does not group that file
with companions. This boundary is also the unit of resume, as described in "Scope and
non-goals" in the README. Therefore, the analyzer must manage a small companion input as a
side-load.

1. **Build the lookup offline.** Survey each companion product into its own inventory,
   using one reader shape per product as described in `docs/ADDING_A_READER.md`. Join that
   inventory to the primary inventory with an explicit matching policy.
   `examples/match_inventories.py` provides a starting point and writes
   `companions.jsonl` keyed by event.
2. **Load the lookup once in `begin()`.** Keep a small mapping from event to a companion
   path already staged on disk, or from event to a CADC URI that will be fetched on
   demand.
3. **Resolve the companion in `consume_file()`.** Archive and local sources both include
   the event in `meta`, so use `meta["event"]` as the lookup key. When fetching on demand,
   cache the result by event. Per-event fan-out then requires one companion fetch per
   product. Remove any companion copy staged by the analyzer, because the engine deletes
   only files that it staged.
4. **Validate the companion during resume.** A different gain solution or other companion
   changes the product meaning. Store its identity in the product and refuse a resume when
   that identity changes.

This side-load pattern is intended for small auxiliary products. If each unit requires a
second bulk input, pre-combine the inputs upstream or use an engine that represents grouped
units. Expanding the side-load would remove the storage bound that this model is designed
to provide.

**Resolve day-keyed archives lazily.** Some companions are organized by day rather than by
event. CHIME calibration gains, for example, use one dataset per date. In this case, the
recon map produced by `--scopes-only --match ... --expand --name gains` supplies the
available days. The analyzer selects a day for each event and resolves its files with one
archive call.

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

`DATATRAIL.files()` provides the programmatic form of `datatrail ps -s`. Import it through
`datatrawl.plugins.sources` rather than parsing command output or importing dtcli
internals. An `ok=False` result means the service did not answer; it does not establish
that the dataset contains no gain file. The analyzer must also define the science policy
for choosing one companion when a day contains several candidates.

[`examples/per_event_companions.py`](../examples/per_event_companions.py) implements steps
2 through 4. It loads the lookup in `begin()`, resolves the companion from
`meta["event"]`, saves the companion identity in the product, and refuses a resume after
that identity is reassigned. The example is exercised through the CLI in
`tests/test_per_event_scan.py`.

## Loading external analyzers

```bash
datatrawl scan --plugin /path/to/my_analyzer.py --analyzer my-analyzer ...
```

or:

```bash
export DATATRAWL_PLUGINS=/path/to/my_analyzer.py
datatrawl scan --analyzer my-analyzer ...
```

or package entry point:

```toml
[project.entry-points."datatrawl.plugins"]
my-analyzer = "my_project.datatrawl_plugins.my_analyzer"
```

After adding or changing an entry point, install or reinstall the package so the
environment contains the current metadata:

```bash
pip install -e /path/to/my_project
```

Loading a Python file by path treats it as a standalone module. It therefore cannot
resolve package-relative imports such as `from . import ...`. If the analyzer imports
sibling modules or shared package helpers, make the package importable with
`pip install -e .` or by adding its root to `PYTHONPATH`. Then load the dotted module name
with `--plugin my_project.my_analyzer`, or declare the entry point shown above.

The
[`WVURAIL/datatrawl-analyzer-template`](https://github.com/WVURAIL/datatrawl-analyzer-template)
repository provides an installable package layout, a declared entry point, and a smoke
suite that runs the engine on synthetic data. Create a repository from that template,
follow its rename checklist, and run `pip install -e .`. The analyzer will then be
discoverable without a `--plugin` flag.

## Preflight checklist

Before using an analyzer at archive scale, run one bounded file and frame through the real
pipeline. Write this smoke-test product to a path that will not be reused for the full
analysis.

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

`--name` expects an inventory built by `datatrawl survey`. For a
credential-free smoke test, use the local source instead:
`--source local --source-root <dir> --telescope <telescope> --reader <reader>`.

Run the same smoke command a second time. A correct resume implementation skips the
completed unit and leaves the accumulated result unchanged. For the full analysis, remove
the bounds and use a fresh output path. The capped and uncapped runs represent different
products and must not be resumed into one file.
