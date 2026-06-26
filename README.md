# datatrawl

[![tests](https://github.com/WVURAIL/datatrawl/actions/workflows/tests.yml/badge.svg)](https://github.com/WVURAIL/datatrawl/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![license: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Storage-safe archive trawling for resumable telescope-data analysis.

`datatrawl` is a downstream run layer for safely scanning Datatrail/CADC data
one file at a time. Datatrail remains the archive authority: it knows which
scopes exist, which datasets are registered, what storage policies apply, and how
a dataset resolves to files. `datatrawl` starts after that archive map exists. It
builds an analysis-specific inventory, verifies the expected units, stages one
file at a time, runs an analyzer, checkpoints the small product, and resumes
after CANFAR/CADC interruptions.

The science itself lives in the analyzer, which is usually your own plugin kept
in your own project.

```text
Datatrail scope(s)
    -> datatrawl survey
        -> inventory.jsonl
        -> inventory.meta.json
            -> datatrawl scan
                -> fetch one unit
                -> reader.iter_arrays(...)
                -> analyzer.consume_file(...)
                -> delete staged file
                -> checkpoint product
```

## The four pieces

| Piece | Meaning |
|---|---|
| **Instrument** | Telescope geometry: band, channelization, Nyquist zone, feed count, NFFT. Pure YAML. |
| **Source** | Where the data lives and how to list + stage it, such as `cadc-datatrail` or `local`. |
| **Reader** | Converts one staged file into arrays + per-file metadata, such as `chime-baseband`. |
| **Analyzer** | The science: consumes arrays and writes a small, resumable product, such as `spectrum`. |

Plus two Datatrail terms a survey works against:

| Term | Meaning |
|---|---|
| **Scope** | Archive namespace, such as `chime.event.baseband.raw`. |
| **Dataset** | A registered name inside a scope. It may be a file-bearing dataset or a larger container. |

A survey turns scopes and datasets into a local **inventory**: a JSONL list of
verified units to scan.

## What Datatrail does

Use Datatrail directly for archive discovery:

```bash
PAGER=cat datatrail ls
PAGER=cat datatrail ls <scope>
PAGER=cat datatrail ls <scope> <dataset>
PAGER=cat datatrail ps <scope> <dataset>
PAGER=cat datatrail ps <scope> <dataset> -s
```

A **larger dataset** can be a container with no files directly attached. A
**file-bearing dataset** shows files under:

```bash
PAGER=cat datatrail ps <scope> <dataset> -s
```

See [`docs/DATATRAIL_BOUNDARY.md`](docs/DATATRAIL_BOUNDARY.md) for what Datatrail
owns versus what `datatrawl` owns.

## Install

```bash
git clone https://github.com/WVURAIL/datatrawl
cd datatrawl

python -m venv .venv
. .venv/bin/activate

pip install -e ".[survey,examples,dev]"
cadc-get-cert -u <your_cadc_username>
```

The `cadc-datatrail` source drives `datatrail`, which needs a one-time site
configuration before the archive example below works:

```bash
datatrail config init --site canfar
```

On CANFAR, use `--site canfar`. Elsewhere, use the site appropriate for your
environment, such as `--site local` or `--site chime`. This writes
`~/.datatrail/config.yaml`.

The offline `pytest` check and the `local` source do not need CADC or Datatrail
configuration. See [datatrail-cli](https://github.com/CHIMEFRB/datatrail-cli) for
the full Datatrail setup.

For GPU analyzers, `datatrawl` uses the CuPy your CANFAR image already ships. If
the image has no CuPy installed, run:

```bash
datatrawl setup-cupy --install
```

This detects the image's CUDA version and installs the matching wheel.

## Verify the install

The worked [CHIME spectrum example](#example-a-chime-single-freq_id-spectrum)
below runs against the real archive, so it needs the CADC certificate from the
install step. The install itself can be checked without an account:

```bash
datatrawl --version   # installed release
datatrawl list        # everything registered
datatrawl doctor      # readiness + the combinations ready to run
pytest -q             # reader -> analyzer -> checkpoint -> resume on synthetic data
```

`pytest` needs no CADC account and no data of your own, so it is the quickest
confirmation that the streaming pipeline works end to end. `make test` and
`make smoke` wrap the tests and the `list` / `doctor` checks.

## Running local files

If you already have baseband `.h5` files on disk, for example staged under
`/arc`, you can run the real pipeline with no survey at all via the `local`
source:

```bash
# What is there?
datatrawl explore --source local --source-root <dir> --telescope chime

# Stage -> analyze -> checkpoint, exactly as a survey-driven scan would.
datatrawl scan --source local --source-root <dir> \
  --telescope chime --reader chime-baseband --analyzer spectrum \
  --select <freq_id> --max-frames-per-file 5
```

Without `--tmp-dir`, each invocation receives a unique scratch directory. The
base directory is chosen from `DATATRAWL_TMPDIR`, then a writable `/scratch`,
then the operating system temporary directory. Pass `--tmp-dir` when a site has
a preferred node-local scratch location. Automatically created directories are
removed after a successful scan when they are empty.

By default, the local source assumes filenames contain the selected `freq_id` as
an integer before `.h5`, for example:

```text
baseband_<event>_<freq_id>.h5
```

The default matching pattern is roughly:

```text
_(\d+)\.h5$
```

For a different local naming convention, pass `--source-freq-id-regex`.

## Commands

Run any command with `--help` for its full options.

| Command | Purpose |
|---|---|
| **`datatrawl list`** | Show registered telescopes, sources, readers, and analyzers, including any loaded with `--plugin`. Start here to see what exists. |
| **`datatrawl doctor`** | Readiness check. On its own, it explains the survey/scan choices and lists ready combinations. With `--telescope ... --source ... --reader ... --analyzer ...`, it gives the prerequisite checklist for one concrete pipeline. |
| **`datatrawl survey`** | Build the run inventory: walk one or more scopes, verify expected files by metadata, omit missing ones, and cache `inventory.jsonl` + `inventory.meta.json`. It does **not** bulk-download archive data. Re-running resumes from the cache. |
| **`datatrawl explore`** | Summarize what a source holds before scanning: freq_ids present, file counts, date span, and total volume. It works on a survey inventory or a local directory. |
| **`datatrawl scan`** | Storage-safe run loop: stage one file to scratch, call the reader, call the analyzer, delete the staged file, and checkpoint the product atomically. Transient fetch failures retry on rerun; unreadable files are quarantined. |

To recover or extend a run, re-run the identical `scan` command.

## Example: a CHIME single-freq_id spectrum

This is a worked end-to-end example. It uses a single scope and a single
`freq_id`, stages a handful of files, and accumulates an averaged power spectrum.
That `freq_id` carries a known narrowband tone, a DTV pilot, at a fixed frequency.
If the tone shows up in the PSD, the whole path works:

```text
survey -> inventory -> scan -> one-file staging -> reader -> analyzer
       -> checkpoint -> resume -> plot
```

This example runs against the real archive, so it needs the CADC certificate from
[Install](#install). With no account yet, use [Verify the install](#verify-the-install)
for an offline path that exercises the same pipeline.

```text
scope:      chime.event.baseband.raw
source:     cadc-datatrail
reader:     chime-baseband
analyzer:   spectrum
selection:  freq_id 844
product:    time- and feed-averaged 2^14-point PSD
```

Build the inventory, inspect it, then run a bounded scan:

```bash
# 1. Build the inventory for a few events.
#    This records the freq_id-844 files; it does not bulk-download data.
datatrawl survey \
  --telescope chime --source cadc-datatrail \
  --scope chime.event.baseband.raw \
  --freq-ids 844 --max-events 5 --name chime-spectrum-844

# 2. Inspect the inventory without downloading bulk data.
datatrawl explore --name chime-spectrum-844

# 3. Stream + reduce a few frames from each event.
#    This is resumable and writes one product for freq_id 844.
datatrawl scan \
  --name chime-spectrum-844 --analyzer spectrum --select 844 \
  --max-frames-per-file 5
```

Plot it:

```bash
# The product is results/chime/spectrum/844.npz.
# Plotting needs matplotlib.
python - <<'PY'
import numpy as np
import matplotlib.pyplot as plt

z = np.load("results/chime/spectrum/844.npz", allow_pickle=False)

f = z["freqs_sky_hz"] / 1e6
psd_db = 10 * np.log10(z["psd"] / np.median(z["psd"]))

plt.plot(f, psd_db, lw=0.7)
plt.xlabel("sky frequency [MHz]")
plt.ylabel("power [dB re median]")
plt.title(f"CHIME freq_id {int(z['freq_id'])}: {int(z['count'])} frames")
plt.savefig("results/chime/spectrum/844.png", dpi=150)
plt.show()
PY
```

A strong narrow feature should appear in the freq_id-844 band at about
470.309 MHz. That is the pilot tone.

Re-run the identical `scan` command to verify resume. It should report that the
product is already complete.

Because this example uses `--max-frames-per-file 5`, the product is deliberately
bounded. For a later uncapped scan, remove the cap and use a fresh `--out` (or
delete the bounded product); `datatrawl` refuses to mix capped and uncapped runs.

On a headless session, such as a CANFAR script rather than a notebook cell,
`plt.show()` may do nothing. The important call is `plt.savefig(...)`.

Some of the oldest events may have aged off CADC storage. The survey reports
those as `resolved-but-empty` and skips them, so the inventory and scan proceed
on whatever is still retrievable. `rows written` can therefore be fewer than
`--max-events`; that is expected, not an error.

## Extending datatrawl for your own analysis

`datatrawl` ships only the generic CHIME spectrum path. Your actual science
usually lives in **your** project as a plugin. You can load that plugin with:

- `--plugin`;
- the `DATATRAWL_PLUGINS` environment variable;
- a package entry point.

Nothing in this repository needs to change for project-specific analysis code.

### Which piece do I need to write?

| Your case | Write this |
|---|---|
| CHIME-compatible baseband, new science product | **Analyzer only** |
| Same files, different statistic / detector / product | **Analyzer only** |
| Files already staged on disk | Usually **analyzer only**; use `--source local` |
| Same telescope, new file format | **Reader + analyzer** |
| New Datatrail scope with a different dataset/file layout | **Source**, possibly **reader**, plus **analyzer** |
| New telescope with CHIME-like files | **Instrument YAML**, possibly **analyzer** |
| New telescope and new file format | **Instrument YAML + reader + analyzer** |

Two realistic examples:

- **An F-statistic DTV pilot detector** using the 23 pilot `freq_id`s on
  `chime.event.baseband.raw` and `chime.scheduled.baseband.raw` needs a new
  **analyzer**. The shipped `cadc-datatrail` source and `chime-baseband` reader
  already deliver the data.
- **A GBO N² burst detector** on `gbo.acquisition.processed`, using all
  `freq_id`s and looking for a short energy spike, likely needs a new
  **source**, a new **reader**, and a new **analyzer**.

### Quick path: using datatrawl from your own project

Most project-specific baseband work only needs a new analyzer. A typical
external project can look like this:

```text
my_project/
    pyproject.toml
    my_project/
        __init__.py
        datatrawl_plugins/
            __init__.py
            my_analyzer.py
```

A self-contained analyzer can be loaded directly by file path:

```bash
datatrawl list analyzers \
  --plugin /path/to/my_project/my_project/datatrawl_plugins/my_analyzer.py
```

A file loaded this way is standalone and cannot use package-relative imports. If
the analyzer imports sibling modules or shared helpers, install the project and
load the analyzer by dotted module name instead:

```bash
pip install -e /path/to/my_project
datatrawl list analyzers \
  --plugin my_project.datatrawl_plugins.my_analyzer
```

Then run a readiness check for the concrete pipeline:

```bash
datatrawl doctor \
  --plugin /path/to/my_project/my_project/datatrawl_plugins/my_analyzer.py \
  --telescope <telescope> \
  --source cadc-datatrail \
  --reader chime-baseband \
  --analyzer my-analyzer
```

After you have surveyed an inventory, run a one-file smoke test before scaling
up. Keep its bounded product separate from the full run:

```bash
datatrawl scan \
  --name <survey-name> \
  --plugin /path/to/my_project/my_project/datatrawl_plugins/my_analyzer.py \
  --analyzer my-analyzer \
  --select <freq_id> \
  --max-files 1 \
  --max-frames-per-file 1 \
  --out smoke/my-analyzer-<freq_id>.npz
```

Run the identical command a second time. It should report that there is nothing
to do and should not duplicate output. For a partial-resume test, run a larger
bounded scan, interrupt it after a checkpoint, and rerun the same command. Use a
fresh output path for the later uncapped analysis.

Once the project is packaged, expose the plugin through an entry point in your
project's `pyproject.toml`:

```toml
[project.entry-points."datatrawl.plugins"]
my-analyzer = "my_project.datatrawl_plugins.my_analyzer"
```

Install or reinstall the project so the entry-point metadata is available:

```bash
pip install -e /path/to/my_project
```

After that, `datatrawl` can discover the plugin without `--plugin`:

```bash
datatrawl list analyzers
datatrawl scan --name <survey-name> --analyzer my-analyzer --select <freq_id>
```

For a concrete external analyzer reference, see
[`examples/external_analyzer.py`](examples/external_analyzer.py).

It lives outside `src/datatrawl/` and is loaded the same way a user project would
load its own analyzer.

### Minimal analyzer shape

An analyzer consumes arrays from a reader and writes a small resumable product.
The most common pattern is to subclass `AccumulatingAnalyzer`:

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

For real analyzers, also validate resume parameters. If changing an option would
change the meaning of the product, save that option into the product and refuse
to resume when it differs. Examples include:

- `freq_id`;
- `nfft`;
- detector threshold;
- window;
- Nyquist zone;
- max frames per file;
- calibration constants.

See [`docs/ADDING_AN_ANALYZER.md`](docs/ADDING_AN_ANALYZER.md) for the full
analyzer contract, including order-dependence, fan-out, plugin loading, and
resume validation.

## Guides

| Guide | Purpose |
|---|---|
| [`docs/ADDING_AN_ANALYZER.md`](docs/ADDING_AN_ANALYZER.md) | Add the science plugin. This is the common case. |
| [`docs/ADDING_A_SOURCE.md`](docs/ADDING_A_SOURCE.md) | Add data from a new location, scope layout, or filesystem convention. |
| [`docs/ADDING_A_READER.md`](docs/ADDING_A_READER.md) | Add support for a new file format. |
| [`docs/ADDING_A_TELESCOPE.md`](docs/ADDING_A_TELESCOPE.md) | Add a new instrument geometry YAML. |
| [`docs/DATATRAIL_BOUNDARY.md`](docs/DATATRAIL_BOUNDARY.md) | Map a use case onto Datatrail + datatrawl responsibilities. |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Long runs, self-healing, quarantine, expired certs, and recovery. |

## Design notes

`datatrawl` talks to Datatrail through its Python API, the functions in
`dtcli.src.functions`, rather than shelling out to the `datatrail` CLI and
scraping its output. The survey step calls those functions directly and gets
structured results back.

The one rough edge is that `dtcli.src.functions` is an internal module, not a
published stable API.

> **Planned upstream improvement, deferred:** propose a machine-readable mode for
> Datatrail's read commands, such as `datatrail ls --json`, writing the same data
> to stdout. Then `datatrawl` can depend on a stable public contract and drop the
> internal-module import entirely. The same idea would help `datatrail ps`. This
> is intentionally held back so it can land as a single, well-scoped PR to
> [CHIMEFRB/datatrail-cli](https://github.com/CHIMEFRB/datatrail-cli) once the
> rest of `datatrawl` is settled. Technical detail lives in the `UPSTREAM NOTE`
> in [`src/datatrawl/plugins/sources/_datatrail.py`](src/datatrawl/plugins/sources/_datatrail.py).

## Release history and citation

Release notes are maintained in [`CHANGELOG.md`](CHANGELOG.md). A machine-readable
software citation is provided in [`CITATION.cff`](CITATION.cff).

The package and runtime report the same version with `datatrawl --version`.
