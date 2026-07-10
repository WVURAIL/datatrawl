# Adding a source

A **source** answers *where the data lives and how to list and stage it*. You add one when
your data is in a new place -- a different Datatrail scope with its own layout, or a
filesystem location the shipped sources don't cover.

This is one of the four pluggable pieces (instrument / source / reader / analyzer); see
[`ADDING_A_READER.md`](ADDING_A_READER.md), [`ADDING_AN_ANALYZER.md`](ADDING_AN_ANALYZER.md),
and [`ADDING_A_TELESCOPE.md`](ADDING_A_TELESCOPE.md) for the others. `datatrawl` is the thin
layer between Datatrail and your analyzer, so a source is usually small;
[`DATATRAIL_BOUNDARY.md`](DATATRAIL_BOUNDARY.md) covers what Datatrail owns versus what a
source owns.

## The two methods

A source implements `enumerate` and `fetch`, deliberately split so the cheap listing step
can be cached and re-run without ever touching bulk data:

```python
from datatrawl.interfaces import DataSource, Unit, PluginInfo, RunContext
from datatrawl.registry import source as register_source


@register_source
class MySource(DataSource):
    info = PluginInfo(
        name="my-source",
        kind="source",
        summary="List + stage <whatever> from <wherever>.",
        needs_archive_config=False,    # True if it pulls from the CADC archive
    )

    def enumerate(self, ctx: RunContext):
        # One Unit per stage-able file. key = stable id (dedup/resume),
        # name = local filename, meta = anything the reader/analyzer needs.
        for rec in my_listing(ctx):
            yield Unit(key=rec.uri, name=rec.filename,
                       meta={"obs_date": rec.date, "freq_id": rec.freq_id})

    def fetch(self, unit: Unit, dest: str):
        try:
            my_download(unit.key, dest)
            return True, ""
        except Exception as exc:
            return False, str(exc)         # logged now; retried when the scan is rerun
```

`Unit.key` is also the default quarantine identity. If a physical fetch URI can change
between surveys while the logical file remains the same, put a stable source-specific value
in `Unit.meta["quarantine_key"]` (for example, an event/channel pair). Do not use a bare
basename unless it is unique within that source.

`survey()` is optional: implement it when `enumerate()` is expensive (a network listing) so
`datatrawl survey` can cache the inventory to disk and later steps reuse it without
re-listing. A cheap source can leave it unimplemented and just enumerate on demand through
`datatrawl explore` and `datatrawl scan`. Running `datatrawl survey` for such a source reports
that a persistent survey is not implemented.

Source-specific settings can be passed through `ctx.options` with repeated `--set key=value`
arguments on `survey`, `explore`, and `doctor`.

## A different archive layout

The shipped `cadc-datatrail` source is an event-keyed CADC/Datatrail source.
For CHIME baseband, it defaults to the `chime-baseband` reader's archive file
shape (`baseband_<event>_<freq_id>.h5`). For another event-keyed product, a new
reader may be enough if that reader can declare `survey_files()`.

Write a new source when the archive layout itself is not event-keyed, when
listing/staging policy differs, when units are not resolved through the
Datatrail event common-path pattern, or when the data source is not
CADC/Datatrail.

The shipped source reaches Datatrail through the public machine-readable CLI
contract, `datatrail ls/ps --json`, available in `datatrail-cli>=0.11.0`. It
verifies candidate files with CADC metadata, writes a persistent inventory,
and later fetches units with CADC. The adapter in
`src/datatrawl/plugins/sources/_datatrail.py` is the single boundary between
datatrawl and Datatrail.

## Registering and loading

The loading mechanism is the same for source, reader, and analyzer plugins. In-tree: drop
the module in `plugins/sources/` and add it to the import list in `plugins/__init__.py`. In
your own project (recommended for project-specific work), keep it in your repo and load it
with:

```bash
datatrawl scan --plugin /path/to/my_source.py ...
# or:
export DATATRAWL_PLUGINS=/path/to/my_source.py
# or an entry point in your package's pyproject.toml:
#   [project.entry-points."datatrawl.plugins"]
#   my-source = "mypkg.my_source"
```

After adding an entry point, install or reinstall the package (`pip install -e .`) so its
metadata is visible. Once loaded, the source shows up in `datatrawl list` / `doctor` and
runs through the same engine (staging, dedup, quarantine, resume) as a built-in.
