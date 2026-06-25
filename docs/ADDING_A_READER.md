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

## Registering and loading

The same for all four piece types. In-tree: drop the module in `plugins/readers/` and add
it to the import list in `plugins/__init__.py`. In your own project (recommended for
project-specific work) keep it in your repo and load it with:

```bash
datatrawl scan --plugin /path/to/my_reader.py ...
# or:
export DATATRAWL_PLUGINS=/path/to/my_reader.py
# or an entry point in your package's pyproject.toml:
#   [project.entry-points."datatrawl.plugins"]
#   my-reader = "mypkg.my_reader"
```

Once loaded it shows up in `datatrawl list` / `doctor` and runs through the same engine
(staging, dedup, quarantine, resume) as a built-in.
