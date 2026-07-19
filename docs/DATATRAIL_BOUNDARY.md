# Datatrail boundary

The clean boundary is simple: `datatrawl` should not try to become Datatrail.

Datatrail owns archive registration and discovery. `datatrawl` turns that
archive view into an analysis inventory and runs the analysis within a bounded
staging footprint.

## Datatrail owns

Datatrail answers questions about the archive:

- What scopes exist?
- Which datasets are registered under a scope?
- Which names are larger datasets and which are file-bearing datasets?
- What policies and storage elements apply?
- What files are attached to a file-bearing dataset?
- How can a dataset be pulled?

Useful commands:

```bash
PAGER=cat datatrail ls
PAGER=cat datatrail ls <scope>
PAGER=cat datatrail ls <scope> <dataset>
PAGER=cat datatrail ps <scope> <dataset>
PAGER=cat datatrail ps <scope> <dataset> -s
PAGER=cat datatrail scout <scope> <dataset>
```

`datatrawl` uses those interfaces rather than duplicating them. Recon
(`survey --scopes-only`) writes the `ls` view to a reusable map. Adding
`--expand` opens each matched name one level and writes any children it finds.
`DATATRAIL.files(scope, dataset)`, imported from
`datatrawl.plugins.sources`, provides the programmatic `ps -s` view and returns
an explicit `ok` value so callers can distinguish an answered empty listing
from a call that did not answer.

There is one important limit to the map. A plain recon row may name a container,
not a file-bearing dataset. Even an expanded map retains a container row when
no children are listed or the child query fails. Treat the map as discovery;
use `files()` or `datatrail ps` to determine whether a row actually has files.

## datatrawl owns

`datatrawl` answers analysis-run questions:

- Which registered files are relevant to this analysis?
- Which expected units were verified for this inventory?
- Which units have already been analyzed?
- How can the analysis run within disk quota?
- How does the run resume after CANFAR/CADC interruptions?
- How is an unreadable unit recorded so a later run can exclude it?

## Survey exists because Datatrail is not an analyzer inventory

Datatrail can show that a dataset is registered. It does not know which rows
your analyzer needs or what should count as one unit of analysis.

Examples:

- A CHIME DTV analysis may request 23 specific baseband `freq_id` files per
  event. The event survey verifies those expected names and writes rows for the
  files it can verify through CADC metadata.
- A GBO processed acquisition may be registered even when the queried dataset
  exposes no files. Recon can preserve the name for investigation, but the
  event survey must not manufacture CHIME-style event/freq_id units for a
  non-event product. That layout needs its own source or reader policy.
- An HDF5 header can fail only after a file has been staged. That is a scan-time
  reader failure, not evidence that the archive registration was wrong. A probe
  failure is recorded in the quarantine ledger when quarantine is enabled.

## Larger datasets versus file-bearing datasets

If:

```bash
datatrail ls gbo.acquisition.processed
```

prints “Larger Datasets”, the names in that table may be containers.

If:

```bash
datatrail ps gbo.acquisition.processed 20230512T012608Z_gbo_corr -s
```

prints an empty file table, that query has not produced a file that can be a
scan unit.

Try:

```bash
PAGER=cat datatrail ls gbo.acquisition.processed 20230512T012608Z_gbo_corr
```

If the child table is also empty, Datatrail has exposed neither files nor child
datasets for that name. Recon's `--expand` performs this child query across the
matched rows and keeps a childless container visible in the map. `files()` is
the outage-aware form of the `ps -s` query: `(None, [], True)` means the call
answered with no usable files, while `ok=False` means the call did not provide a
usable answer. Neither case should be converted into invented scan units.

## Required user knowledge

Before writing a new analysis, map out the following questions. Neither example
analyzer in this table ships with `datatrawl`; they show where the archive,
format, and science responsibilities separate.

| Question | Example: a CHIME DTV detector | Example: a GBO processed-acquisition scan |
|---|---|---|
| Which scope(s)? | `chime.event.baseband.raw`, `chime.scheduled.baseband.raw` | `gbo.acquisition.processed` |
| What does `datatrail ls <scope>` list? | event-like groupings for baseband | larger datasets like `20230512T012608Z_gbo_corr` |
| What is one scan unit? | one `baseband_<event>_<freq_id>.h5` | unknown until a nonempty processed dataset is found |
| What selection exists? | CHIME `freq_id` | likely all files/frequencies |
| Which reader parses it? | `chime-baseband` (shipped) | a reader you write for the processed files |
| Which analyzer runs? | an F-statistic analyzer | an energy-spike analyzer |

## Design rules

1. Instrument YAML may provide default event-survey scopes, but an explicit
   `--scope` must remain available. Do not treat those defaults as a complete
   map of the telescope's archive namespace.
2. Do not use CHIME baseband freq_id probing for non-baseband products.
3. Keep Datatrail traversal (the source) separate from file-format parsing
   (the reader) and science (the analyzer).
4. Keep file-format knowledge in readers.
5. Keep science logic in analyzers.
6. Keep corrupt-file quarantine in scan, not survey.
7. For a resumable analyzer, persist committed unit keys and reject an
   incompatible existing product in `resume()`.
