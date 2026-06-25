# Adding an analyzer

An analyzer is the science plugin. It consumes arrays from a reader and writes a small,
resumable product.

The public and internal plugin type is `analyzer`.

## Minimal analyzer

```python
from datatrawl.interfaces import Analyzer, PluginInfo, RunContext
from datatrawl.analyzer_base import AccumulatingAnalyzer
from datatrawl.registry import analyzer as register_analyzer


@register_analyzer
class MyAnalyzer(AccumulatingAnalyzer):
    info = PluginInfo(
        name="my-analyzer",
        kind="analyzer",
        summary="Accumulate a small product from streamed arrays.",
        instruments=("*",),
        produces="my-analyzer/<selection>.npz",
        requires=("numpy",),
    )

    def begin(self, ctx: RunContext, first_meta):
        self._count = 0
        self._sum = 0.0

    def consume_file(self, arrays, meta):
        n = 0
        for arr in arrays:
            self._sum += float(arr.mean())
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

Loading a single file by path (`--plugin .../my_analyzer.py`) cannot resolve
package-relative imports (`from . import ...`): the file is loaded standalone, with
no parent package. If your plugin lives in a package (importing siblings or shared
helpers), make the package importable (`pip install -e .`, or put its root on
`PYTHONPATH`) and load it by module name (`--plugin my_project.my_analyzer`) or via
the entry point above.

## Preflight checklist

Before archive scale:

```bash
datatrawl list analyzers
datatrawl doctor --telescope <instrument> --source <source> --reader <reader> --analyzer <name>
datatrawl scan --name <inventory> --analyzer <name> --max-files 1 --max-frames-per-file 1
```

Then interrupt and rerun the identical command. It should skip completed units and avoid
duplicating output rows.
