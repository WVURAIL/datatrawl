# Adding a reader

A **reader** turns one staged file into the stream items that an analyzer consumes. Add a reader
when the file format differs from the shipped `chime-baseband` format. The difference may
be the dataset layout, sample packing, data type, or metadata attributes.

The run pipeline has four pluggable parts: telescope, source, reader, and analyzer. The
reader owns the on-disk format. The other extension points are described in
[`ADDING_A_SOURCE.md`](ADDING_A_SOURCE.md),
[`ADDING_AN_ANALYZER.md`](ADDING_AN_ANALYZER.md), and
[`ADDING_A_TELESCOPE.md`](ADDING_A_TELESCOPE.md).

## The two methods

A reader implements two methods. `probe` reads the small metadata needed to plan the
analysis, and `iter_arrays` yields the data items in streaming order.

```python
from datatrawl.interfaces import (
    Reader, PluginInfo, RunContext, STREAM_COMPLEX_BASEBAND,
)
from datatrawl.registry import reader as register_reader


@register_reader
class MyReader(Reader):
    info = PluginInfo(name="my-reader", kind="reader",
                      summary="Parse <format> into frames.",
                      stream_kind=STREAM_COMPLEX_BASEBAND)

    def probe(self, path):
        # cheap per-file metadata, WITHOUT reading bulk data
        return {"f_center_hz": ..., "fs_hz": ..., "nfft": ...}

    def iter_arrays(self, path, ctx: RunContext):
        # yield the arrays ("frames") in streaming order
        for frame in read_frames(path):
            yield frame
```

`probe` returns metadata such as centre frequency, sample rate, and frame length without
reading the bulk array. The engine consumes the iterator returned by `iter_arrays`, so the
reader can decode the file incrementally instead of materializing the complete file.

### Classify unreadable bytes explicitly

Raise `datatrawl.interfaces.UnreadableUnitError` only when the reader has established a
deterministic problem with that staged unit's bytes, header, or schema. That explicit
disposition makes the unit eligible for the persistent quarantine ledger. An ordinary
`OSError`, dependency error, or programming exception aborts the run without quarantine,
so a reader bug cannot cause good data to be skipped permanently.

Wrap only the expected format exceptions and preserve their cause:

```python
from datatrawl.interfaces import UnreadableUnitError


try:
    header = parse_header(path)
except (OSError, KeyError, ValueError) as exc:
    raise UnreadableUnitError(f"invalid header in {path}: {exc}") from exc
```

An `UnreadableUnitError` from `probe()` occurs before analyzer state changes, so the engine
can quarantine that unit and continue. If it is raised while `iter_arrays()` is yielding,
the analyzer may already contain partial in-memory updates; the engine records the
quarantine, aborts, and does not checkpoint that state. The next run resumes from the last
completed product and excludes the quarantined unit.

The semantic stream kind and array shape form the contract between the reader and
analyzer. The engine passes each item through without interpreting its dimensions, but
`doctor` and `scan` compare the reader's `stream_kind` with the analyzer's declared
`accepts_stream_kinds` before approving the pair. The CHIME baseband reader yields
`[nfft, n_feeds]` frames so an analyzer can combine feeds. A beamformed reader may instead
yield one `[nfft]` stream. Both forms work when the selected analyzer expects the reader's
shape and declares the same semantic kind.

Use a shipped stream constant when its documented semantics fit. A project-specific
stream should use a package-qualified, versioned name such as
`"my_project.calibration-solution/v1"`. Every reader must declare a non-empty
`stream_kind`. Registration/validation reports a missing declaration as an error, and
`doctor`/`scan` will not approve the reader until its output contract is explicit.

The implementation in `src/datatrawl/plugins/readers/chime_baseband.py` is the primary
reference. Its 4+4-bit unpacking is implemented in `_baseband_format.py`.

## Archive file shape (optional): making a product surveyable

`probe` and `iter_arrays` are sufficient when the files already appear in a local listing
or inventory. An archive survey has one additional question: which files should one event
contribute? For an event-keyed product, the reader answers that question with its
**archive file shape**. We keep the file naming with the reader so survey and scan use the
same definition.

This pattern requires one event to resolve to one common archive path. Day-keyed,
timestamped, and container-style products do not fit that event survey. For those layouts,
build a discovery map first and resolve the files in an analyzer or custom source.

The following registered reader is a complete event-keyed example. It expects an HDF5
dataset named `gains`; replace that dataset contract with the format being added.

```python
import h5py

from datatrawl.interfaces import (
    Reader, PluginInfo, RunContext, UnreadableUnitError,
)
from datatrawl.registry import reader as register_reader


@register_reader
class GainsReader(Reader):
    info = PluginInfo(name="chime-gains", kind="reader",
                      summary="Per-event gain solutions (HDF5).",
                      instruments=("chime",),
                      stream_kind="my_project.gain-solution/v1")
    survey_schema = 1
    minimum_archive_bytes = 512

    def probe(self, path):
        # Cheap metadata only; do not read the bulk array here.
        try:
            with h5py.File(path, "r") as f:
                return {"kind": "gains", "shape": tuple(f["gains"].shape)}
        except (OSError, KeyError, ValueError) as exc:
            raise UnreadableUnitError(f"invalid gains file {path}: {exc}") from exc

    def iter_arrays(self, path, ctx: RunContext):
        try:
            with h5py.File(path, "r") as f:
                gains = f["gains"]
                for i in range(gains.shape[0]):
                    yield gains[i]
        except (OSError, KeyError, ValueError) as exc:
            raise UnreadableUnitError(f"cannot decode {path}: {exc}") from exc

    def survey_files(self, event, common_path, selection, ctx):
        # (filename, fields) per candidate file. Baseband yields one per
        # freq_id with {"freq_id": ch}; a per-event product yields one file.
        # `filename` is relative to the event's common path (sub-paths are
        # fine); `fields` land verbatim as columns in the inventory row.
        yield f"gains_{event}.h5", {"kind": "gains"}

```

`minimum_archive_bytes` is an archive-survey validation hook, not a generic
engine limit. `cadc-datatrail` accepts a candidate inventory row only when CADC
reports at least this many bytes. The default is 1 byte; `chime-baseband` uses
1 MiB because a smaller object cannot be a useful baseband file. Set a positive
integer on the reader, or a callable `minimum_archive_bytes(ctx)` when the floor
depends on run context. Local scans and the streaming engine do not apply this
size rule; format validity still belongs in `probe()`.

Every reader used to create an archive inventory must declare a positive integer
`survey_schema` (start at `1`). This versions the reader's inventory-row contract, not the
package or data format. Increment it whenever `survey_files()`, `annotate_row()`, required
row fields, or their meanings change incompatibly. The built-in `cadc-datatrail` source
records the value in `survey_manifest.json` and refuses to append rows produced under a
different schema. A reader used only for local/pre-existing listings does not need an
archive survey schema.

For shape-changing reader settings that are not already represented by ordinary
JSON-compatible run options, override the optional `Reader.survey_fingerprint(ctx)` hook.
Return a small JSON-compatible value that completely identifies those settings. The
built-in `cadc-datatrail` source stores it in `survey_manifest.json` and refuses to append
when it changes. This is a CADC survey-persistence hook, not part of local enumeration or
the streaming reader contract; keep the default when the reader's type and options
already describe the archive shape.

Load the reader and use it to build an inventory. The `--scope` value identifies the
Datatrail scope that contains the product. Recon with `--scopes-only --match <term>` can
locate that scope. If the match is a container, `--expand` opens it one level so
`scopes.jsonl` contains the file-bearing datasets, such as timestamped acquisitions under
`complex_gains`.

```bash
datatrawl survey --telescope chime --reader chime-gains \
    --plugin /path/to/gains_reader.py \
    --scope <the.gains.scope> --name chime-gains
```

Each verified inventory row records the file's `name`. The later `enumerate` and `fetch`
steps therefore stage the name that survey verified. When `--reader` is omitted, survey
uses the telescope's canonical reader, which preserves the existing baseband command
path. Current CADC inventories must be self-describing: a missing `name` (or any other
required identity field) is rejected with instructions to rebuild the inventory. The
source does not guess a filename from a legacy row.

The source passes its resolved per-survey specification as `selection`. A baseband survey,
for example, passes the `--freq-ids` list. A reader whose archive shape does not use that
selection can ignore it. The default `survey_files` method raises an actionable error when
a survey calls it, while the default `annotate_row` method makes no change. A reader used
only with existing inventories or local files does not need to override either method.

For a production reference, see `survey_files` and `annotate_row` on
`ChimeBasebandReader` in `chime_baseband.py`. The round-trip behavior is tested in
`tests/test_event_selection_and_shape.py`.

## Registering and loading

Readers use the same loading paths as sources and analyzers. For an in-tree reader, add the
module to `plugins/readers/` and import it from `plugins/__init__.py`. For project-specific
work, keep the reader in the project that uses it and load it with one of the following
forms:

```bash
datatrawl scan --plugin /path/to/my_reader.py ...
# or:
export DATATRAWL_PLUGINS=/path/to/my_reader.py
# or an entry point in your package's pyproject.toml:
#   [project.entry-points."datatrawl.plugins"]
#   my-reader = "mypkg.my_reader"
```

After adding or changing an entry point, install or reinstall the package with
`pip install -e .` so the environment contains the current metadata. The reader then
appears in `datatrawl list` and `datatrawl doctor`. External and built-in readers use the
same bounded-staging, quarantine, and resume paths. Duplicate units are an enumeration
concern owned by the source, not the reader.
