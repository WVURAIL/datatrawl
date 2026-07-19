# Adding a source

A **source** defines where files live, how they are listed, and how one file is staged for
analysis. Add a source when the shipped sources do not match the archive layout or
filesystem policy. An event-keyed Datatrail product with a new file format may need only a
reader, so check that boundary before adding source code.

The run pipeline has four pluggable parts: telescope, source, reader, and analyzer. The
source is the archive-access part. [`DATATRAIL_BOUNDARY.md`](DATATRAIL_BOUNDARY.md)
describes the division between Datatrail and `datatrawl`. The remaining extension points
are covered in [`ADDING_A_READER.md`](ADDING_A_READER.md),
[`ADDING_AN_ANALYZER.md`](ADDING_AN_ANALYZER.md), and
[`ADDING_A_TELESCOPE.md`](ADDING_A_TELESCOPE.md).

## The two methods

A source implements `enumerate` and `fetch`. We separate listing from transfer so an
inventory can be reused without downloading the bulk data again.

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
        # Emit each logical file once. key = stable resume identity,
        # name = local filename, meta = fetch details + analysis metadata.
        for rec in my_listing(ctx):
            yield Unit(key=rec.logical_id, name=rec.filename,
                       meta={"fetch_uri": rec.uri, "obs_date": rec.date,
                             "freq_id": rec.freq_id})

    def fetch(self, unit: Unit, dest: str):
        try:
            my_download(unit.meta["fetch_uri"], dest)
            return True, ""
        except Exception as exc:
            return False, str(exc)         # logged now; retried when the scan is rerun
```

`Unit.key` is the stable logical identity used for resume. The engine skips keys already
committed to a saved product, but it does not collapse duplicate keys within one call to
`enumerate()`. The source must therefore emit each logical unit once, deduplicating its
own listing when necessary.

A physical fetch URI may change while the logical file remains the same. Keep the stable
identity in `Unit.key`, store the current URI in metadata, and have `fetch()` use that URI.
`Unit.key` is also the default quarantine identity. `Unit.meta["quarantine_key"]` can
override the quarantine ledger key, but it does not change resume identity. A bare
basename is suitable only when it is unique within the source.

Implement `survey()` when `enumerate()` requires an expensive network listing. The survey
writes a persistent inventory, and later commands can reuse it without repeating that
listing. A source with a cheap listing can leave `survey()` unimplemented and enumerate on
demand through `datatrawl explore` or `datatrawl scan`. For that source, `datatrawl survey`
reports that persistent survey output is not implemented.

Pass source-specific settings through `ctx.options`. The `doctor`, `survey`, `explore`, and
`scan` commands all accept repeated `--set key=value` arguments.

## A different archive layout

The shipped `cadc-datatrail` source follows an event-keyed CADC/Datatrail layout. For CHIME
baseband, it uses the archive file shape declared by the `chime-baseband` reader:
`baseband_<event>_<freq_id>.h5`. Another event-keyed product can use the same source when
its reader defines `survey_files()`.

A new source is needed when units do not resolve through the Datatrail event common-path
pattern. This includes non-event-keyed archives, different listing or staging policies,
and data outside CADC/Datatrail.

The `cadc-datatrail` source accesses Datatrail through the public machine-readable
`datatrail ls/ps --json` interface in `datatrail-cli>=0.11.0`. It verifies candidate files
with CADC metadata, writes the inventory, and later stages selected units with CADC. We
keep this integration in `src/datatrawl/plugins/sources/_datatrail.py` so the Datatrail
boundary has one implementation.

## Registering and loading

Sources use the same loading paths as readers and analyzers. For an in-tree source, add the
module to `plugins/sources/` and import it from `plugins/__init__.py`. For project-specific
work, keep the source in the project that uses it and load it with one of the following
forms:

```bash
datatrawl scan --plugin /path/to/my_source.py ...
# or:
export DATATRAWL_PLUGINS=/path/to/my_source.py
# or an entry point in your package's pyproject.toml:
#   [project.entry-points."datatrawl.plugins"]
#   my-source = "mypkg.my_source"
```

After adding or changing an entry point, install or reinstall the package with
`pip install -e .` so the environment contains the current metadata. The loaded source then
appears in `datatrawl list` and `datatrawl doctor`. External and built-in sources use the
same bounded-staging, quarantine, and resume engine. Each source remains responsible for
emitting a duplicate-free enumeration.
