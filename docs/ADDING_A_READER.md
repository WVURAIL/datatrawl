# Adding a reader

A **reader** turns one staged file into the arrays (frames) an analyzer consumes. You add
one when your files are in a format the shipped `chime-baseband` reader doesn't understand
-- a different dataset layout, dtype packing, or set of attributes.

This is one of the four pluggable pieces (instrument / source / reader / analyzer); see
[`ADDING_A_SOURCE.md`](ADDING_A_SOURCE.md), [`ADDING_AN_ANALYZER.md`](ADDING_AN_ANALYZER.md),
and [`ADDING_A_TELESCOPE.md`](ADDING_A_TELESCOPE.md) for the others.

## The two methods

A reader owns the on-disk format knowledge (dataset names, dtype packing, attribute
conventions) and implements `probe` and `iter_arrays`:

```python
from datatrawl.interfaces import Reader, PluginInfo, RunContext
from datatrawl.registry import reader as register_reader


@register_reader
class MyReader(Reader):
    info = PluginInfo(name="my-reader", kind="reader",
                      summary="Parse <format> into frames.")

    def probe(self, path):
        # cheap per-file metadata, WITHOUT reading bulk data
        return {"f_center_hz": ..., "fs_hz": ..., "nfft": ...}

    def iter_arrays(self, path, ctx: RunContext):
        # yield the arrays ("frames") in streaming order
        for frame in read_frames(path):
            yield frame
```

`probe` returns the small metadata an analyzer needs (centre frequency, sample rate, frame
length); `iter_arrays` streams the data so the engine never holds a whole file in memory.

The shape of the arrays a reader yields is a private contract between it and its analyzer --
the engine passes them through unchanged and never inspects them. CHIME baseband yields
`[nfft, n_feeds]` frames (many feeds to combine); a beamformed format might yield `[nfft]`
frames (a single stream). Either is fine, as long as the analyzer paired with the reader
expects that shape.

Reference: `src/datatrawl/plugins/readers/chime_baseband.py` (a worked reader, with the
4+4-bit unpacking in `_baseband_format.py`).

## Archive file shape (optional): making a product surveyable

`probe`/`iter_arrays` are enough to scan files that are already listed (a local directory,
or an existing inventory). To let `datatrawl survey` build an inventory of YOUR product
from the archive, the reader also declares its **archive file shape** -- which files one
event contributes, and what they are named. That is format knowledge, so it lives on the
same class that will later open the files; survey and read then share one naming
definition and cannot drift:

This survey pattern applies when the archive product is event-keyed: one event
resolves to one common path, and the reader can name that event's expected
files. For day-keyed, timestamped, or container-style products, build a
discovery map first and resolve files from an analyzer or a custom source
instead of forcing the event-survey model.

The following is a complete registered skeleton. It assumes an HDF5 dataset
named `gains`; replace that dataset contract with the one your format uses.

```python
import h5py

from datatrawl.interfaces import Reader, PluginInfo, RunContext
from datatrawl.registry import reader as register_reader


@register_reader
class GainsReader(Reader):
    info = PluginInfo(name="chime-gains", kind="reader",
                      summary="Per-event gain solutions (HDF5).",
                      instruments=("chime",))

    def probe(self, path):
        # Cheap metadata only; do not read the bulk array here.
        with h5py.File(path, "r") as f:
            return {"kind": "gains", "shape": tuple(f["gains"].shape)}

    def iter_arrays(self, path, ctx: RunContext):
        with h5py.File(path, "r") as f:
            gains = f["gains"]
            for i in range(gains.shape[0]):
                yield gains[i]

    def survey_files(self, event, common_path, selection, ctx):
        # (filename, fields) per candidate file. Baseband yields one per
        # freq_id with {"freq_id": ch}; a per-event product yields one file.
        # `filename` is relative to the event's common path (sub-paths are
        # fine); `fields` land verbatim as columns in the inventory row.
        yield f"gains_{event}.h5", {"kind": "gains"}

```

Then survey with it (`--plugin` loads an external module; `--scope` names where the
product lives in Datatrail -- recon with `--scopes-only --match <term>` finds that, and
`--expand` opens a container hit one level so `scopes.jsonl` lists the actual product
datasets, e.g. the timestamped acquisitions under `complex_gains`):

```bash
datatrawl survey --telescope chime --reader chime-gains \
    --plugin /path/to/gains_reader.py \
    --scope <the.gains.scope> --name chime-gains
```

Each verified row records the file's `name`, so `enumerate`/`fetch` stage exactly what
survey verified, whatever the shape. `--reader` defaults to the telescope's canonical
reader, which keeps every existing baseband survey invocation byte-identical. Inventories
written before rows carried `name` still work: enumerate falls back to the baseband
naming for them.

`selection` is whatever per-survey spec the source resolved (baseband gets the
`--freq-ids` list); a shape that is not selected that way ignores it. The two methods have
working defaults on `Reader` -- `survey_files` refuses with an actionable error (a reader
that never surveys needs neither), `annotate_row` does nothing.

Reference: the baseband shape on `ChimeBasebandReader` (`survey_files` + `annotate_row` in
`chime_baseband.py`), and the round-trip test in
`tests/test_event_selection_and_shape.py`.

## Registering and loading

The loading mechanism is the same for source, reader, and analyzer plugins. In-tree: drop
the module in `plugins/readers/` and add it to the import list in `plugins/__init__.py`. In
your own project (recommended for project-specific work), keep it in your repo and load it
with:

```bash
datatrawl scan --plugin /path/to/my_reader.py ...
# or:
export DATATRAWL_PLUGINS=/path/to/my_reader.py
# or an entry point in your package's pyproject.toml:
#   [project.entry-points."datatrawl.plugins"]
#   my-reader = "mypkg.my_reader"
```

After adding an entry point, install or reinstall the package (`pip install -e .`) so its
metadata is visible. Once loaded, the reader shows up in `datatrawl list` / `doctor` and
runs through the same engine (staging, dedup, quarantine, resume) as a built-in.
