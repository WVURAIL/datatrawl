# datatrawl

[![tests](https://github.com/WVURAIL/datatrawl/actions/workflows/tests.yml/badge.svg)](https://github.com/WVURAIL/datatrawl/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![license: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Storage-safe archive trawling for resumable telescope-data analysis.

`datatrawl` is a downstream run layer for safely scanning Datatrail/CADC data
one file at a time. It does **not** replace Datatrail.

It does **not** replace Datatrail. Datatrail is the archive authority: it knows which
scopes exist, which datasets are registered, what storage policies apply, and how a
dataset resolves to files. `datatrawl` starts after that archive map exists. It builds
an analysis-specific inventory, verifies the expected units, stages one file at a time,
runs an analyzer, checkpoints the small product, and resumes after CANFAR/CADC
interruptions. The science itself lives in the analyzer -- which is usually your own
plugin, kept in your own project.

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
| **Source** | Where the data lives and how to list + stage it (e.g. `cadc-datatrail`, `local`). |
| **Reader** | Converts one staged file into arrays + per-file metadata (e.g. `chime-baseband`). |
| **Analyzer** | The science: consumes arrays and writes a small, resumable product (e.g. `spectrum`). |

Plus two Datatrail terms a survey works against: a **scope** (archive namespace such as
`chime.event.baseband.raw`) and a **dataset** (a registered name inside a scope, which
may be a file-bearing dataset or a larger container). A survey turns those into a local
**inventory** (a JSONL list of verified units to scan).

## What Datatrail does

Use Datatrail directly for archive discovery:

```bash
PAGER=cat datatrail ls
PAGER=cat datatrail ls <scope>
PAGER=cat datatrail ls <scope> <dataset>
PAGER=cat datatrail ps <scope> <dataset>
PAGER=cat datatrail ps <scope> <dataset> -s
```

A **larger dataset** can be a container with no files directly attached; a **file-bearing
dataset** shows files under `datatrail ps <scope> <dataset> -s`. See
[`docs/DATATRAIL_BOUNDARY.md`](docs/DATATRAIL_BOUNDARY.md) for what Datatrail owns versus
what `datatrawl` owns.

## Install

```bash
git clone https://github.com/WVURAIL/datatrawl
cd datatrawl

python -m venv .venv
. .venv/bin/activate

pip install -e ".[survey,examples,dev]"
cadc-get-cert -u <your_cadc_username>
```

The `cadc-datatrail` source drives `datatrail`, which needs a one-time site config before
the archive example below works: `datatrail config init --site canfar` on CANFAR (or
`--site local` / `--site chime` elsewhere) writes `~/.datatrail/config.yaml`. The offline
`pytest` check and the `local` source need none of this; see
[datatrail-cli](https://github.com/CHIMEFRB/datatrail-cli) for the full setup.

For GPU analyzers, `datatrawl` uses the CuPy your CANFAR image already ships; if it has
none, run `datatrawl setup-cupy --install` to detect the image's CUDA version and
install the matching wheel.

## Verify the install

The worked [example](#example-a-chime-single-freq_id-spectrum) below runs against the real
archive, so it needs the CADC cert from the step above. The install itself can be checked
without an account:

```bash
datatrawl list        # everything registered
datatrawl doctor      # readiness + the combos ready to run
pytest -q             # full reader -> analyzer -> checkpoint -> resume path on synthetic data
```

`pytest` needs no CADC account and no data of your own, so it is the quickest confirmation
the streaming pipeline works end to end. (`make test` and `make smoke` wrap the tests and
the `list`/`doctor` checks.)

If you already have baseband `.h5` files on disk -- e.g. staged under `/arc` -- you can run
the real pipeline with no survey at all via the `local` source:

```bash
# what is there?
datatrawl explore --source local --source-root <dir> --telescope chime

# stage -> analyze -> checkpoint, exactly as a survey-driven scan would
datatrawl scan --source local --source-root <dir> \
  --telescope chime --reader chime-baseband --analyzer spectrum \
  --select <freq_id> --max-frames-per-file 5
```

## Commands

Run any command with `--help` for its full options.

- **`datatrawl list`** -- everything registered: telescopes, sources, readers, and analyzers
  (including any loaded with `--plugin`). Start here to see what's available.
- **`datatrawl doctor`** -- the readiness check: on its own it explains the survey/scan
  choices and lists the source/reader/analyzer combos ready to run. Add `--telescope ...
  --source ... --reader ... --analyzer ...` to get the exact prerequisite checklist (Python
  extras, CADC cert, GPU) for that one pipeline, end to end.
- **`datatrawl survey`** -- build the run's inventory: walk one or more scopes, verify
  expected files by metadata (omitting missing ones), and cache `inventory.jsonl` +
  `inventory.meta.json` to disk. It does **not** bulk-download archive data, and re-running
  resumes from the cache.
- **`datatrawl explore`** -- summarize what a source holds before scanning (freq_ids
  present, file counts, date span, total volume): a survey's inventory for an archive
  source, or a directory for `--source local`. Use it to pick a subset before committing.
- **`datatrawl scan`** -- the storage-safe run loop: stage one file to scratch, call the
  reader, call the analyzer, delete the staged file, and checkpoint the product atomically.
  Transient fetch failures retry on rerun; files that download but cannot be read are
  quarantined. To recover or extend a run, **re-run the identical scan command.**

## Example: a CHIME single-freq_id spectrum

This is an example worked end-to-end. It uses a single scope and a single freq_id, stages a
handful of files, and accumulates an averaged power spectrum. That freq_id carries a known
narrowband tone (a DTV pilot) at a fixed frequency; if the tone shows up in the PSD, the
whole path -- survey, one-file staging, reader, analyzer, checkpoint, resume, plot -- is
working. It runs against the real archive, so it needs the CADC cert from
[Install](#install); with no account yet, see [Verify the install](#verify-the-install) for
an offline path that exercises the same pipeline.

```text
scope:      chime.event.baseband.raw
source:     cadc-datatrail
reader:     chime-baseband
analyzer:   spectrum
selection:  freq_id 844
product:    time- and feed-averaged 2^14-point PSD
```

Build the inventory, then run a bounded scan:

```bash
# 1. build the inventory for a few events (records the freq_id-844 files; no bulk download)
datatrawl survey \
  --telescope chime --source cadc-datatrail \
  --scope chime.event.baseband.raw \
  --freq-ids 844 --max-events 5 --name chime-spectrum-844

# 2. inspect it without downloading bulk data
datatrawl explore --name chime-spectrum-844

# 3. stream + reduce a few frames from each of those events (resumable; one product per freq_id)
datatrawl scan \
  --name chime-spectrum-844 --analyzer spectrum --select 844 \
  --max-frames-per-file 5
```

Plot it:

```bash
# the product is results/chime/spectrum/844.npz; plot it (needs matplotlib)
python - <<'PY'
import numpy as np, matplotlib.pyplot as plt
z = np.load("results/chime/spectrum/844.npz", allow_pickle=False)
f = z["freqs_sky_hz"] / 1e6
plt.plot(f, 10*np.log10(z["psd"]/np.median(z["psd"])), lw=0.7)
plt.xlabel("sky frequency [MHz]"); plt.ylabel("power [dB re median]")
plt.title(f"CHIME freq_id {int(z['freq_id'])}: {int(z['count'])} frames")
plt.show(); plt.savefig("results/chime/spectrum/844.png")
PY
```

A strong narrow feature should appear in the freq_id-844 band at ~470.309 MHz -- that is
the "pilot tone." Re-run the identical `scan` command to verify resume; it should report the
product is already complete. On a headless session -- a script rather than a notebook cell,
as is common on CANFAR -- `plt.show()` does nothing; save the figure with
`plt.savefig("spectrum.png")` instead.

Some of the oldest events may have aged off CADC storage; the survey reports those as
`resolved-but-empty` and skips them, so the inventory (and the scan) proceed on whatever is
still retrievable. `rows written` can therefore be fewer than `--max-events` -- that is
expected, not an error.

## Extending datatrawl for your own analysis

`datatrawl` ships only the generic CHIME spectrum path. Your actual analysis lives in
**your** project as a plugin, loaded with `--plugin`, the `DATATRAWL_PLUGINS` env var, or a
package entry point -- nothing in this repo changes. Two real examples:

- **An F-statistic DTV pilot detector** (the 23 pilot freq_ids on `chime.event.baseband.raw`
  `chime.scheduled.baseband.raw`, all events/frames): a new **analyzer**. The shipped
  `cadc-datatrail` source and `chime-baseband` reader already deliver the data, so this is
  the only piece to write; `src/datatrawl/plugins/analyzers/spectrum.py` is the worked
  reference, and `docs/ADDING_AN_ANALYZER.md` covers loading it from your own repo.
- **A GBO N^2 burst detector** (`gbo.acquisition.processed`, all freq_ids,
  looking for a short energy spike): a new **source** (to enumerate that scope) + **reader**
  **analyzer**.

Guides:

- [`docs/ADDING_AN_ANALYZER.md`](docs/ADDING_AN_ANALYZER.md) -- the science plugin (the common case)
- [`docs/ADDING_A_SOURCE.md`](docs/ADDING_A_SOURCE.md) -- data in a new location (scope or filesystem)
- [`docs/ADDING_A_READER.md`](docs/ADDING_A_READER.md) -- data in a new file format
- [`docs/ADDING_A_TELESCOPE.md`](docs/ADDING_A_TELESCOPE.md) -- a new instrument geometry (YAML)
- [`docs/DATATRAIL_BOUNDARY.md`](docs/DATATRAIL_BOUNDARY.md) -- how to map a use case onto Datatrail + datatrawl
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)

## Design notes

`datatrawl` talks to Datatrail through its Python API (the functions in `dtcli.src.functions`)
rather than shelling out to the `datatrail` CLI and scraping its output: the survey step calls
those functions directly and gets structured results back. The one rough edge is that
`dtcli.src.functions` is an internal module, not a published, stable API.

> **Planned upstream improvement (deferred):** propose a machine-readable mode for
> Datatrail's read commands — e.g. `datatrail ls --json` writing the same data to stdout —
> so `datatrawl` can depend on a stable, public contract and drop the internal-module
> import entirely (the same idea would help `datatrail ps`). This is intentionally held
> back so it can land as a single, well-scoped PR to
> [CHIMEFRB/datatrail-cli](https://github.com/CHIMEFRB/datatrail-cli) once the rest of
> `datatrawl` is settled. Technical detail lives in the `UPSTREAM NOTE` in
> [`src/datatrawl/plugins/sources/_datatrail.py`](src/datatrawl/plugins/sources/_datatrail.py).
