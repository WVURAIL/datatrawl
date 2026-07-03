# Datatrail boundary

`datatrawl` should not reinvent Datatrail.

Datatrail owns archive registration and discovery. `datatrawl` owns analysis inventories
and storage-safe execution.

## Datatrail owns

Datatrail answers:

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

datatrawl reaches this hierarchy without reinventing it: recon
(`survey --scopes-only`, with `--expand` to open a container one level) writes
the `ls` view as a reusable map, and `DATATRAIL.files(scope, dataset)` --
importable from `datatrawl.plugins.sources` -- is the programmatic `ps -s`,
with a contract that never lets a service outage read as emptiness.

## datatrawl owns

`datatrawl` answers:

- Which registered files are relevant to this analysis?
- Which expected units are present?
- Which files are missing and should be omitted?
- Which files have already been analyzed?
- How can the analysis run within disk quota?
- How does the run resume after CANFAR/CADC interruptions?
- How does one bad file get quarantined without stopping a campaign?

## Survey exists because Datatrail is not an analyzer inventory

Datatrail can show that a dataset is registered. It does not decide what your analyzer
expects.

Examples:

- CHIME DTV needs 23 specific baseband `freq_id` files per event. Some events may be
  missing some freq_ids. Survey should include present files and omit missing ones.
- GBO processed acquisition may have registered datasets with policies but no visible files.
  Survey should record them as empty/skipped, not manufacture event/freq_id probes.
- A rare HDF5 bad header is not a survey problem. The file exists and may download; the
  reader discovers the bad header during scan, and scan quarantines it.

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

prints an empty file table, then that name is not directly usable as a scan unit.

Try:

```bash
PAGER=cat datatrail ls gbo.acquisition.processed 20230512T012608Z_gbo_corr
```

If the child dataset table is empty too, then the registered dataset appears empty through
Datatrail and survey should skip it. Recon's `--expand` runs exactly this
child check across every matched dataset at once, and `files()` is the
empty-versus-outage-aware form of the `ps -s` probe.

## Required user knowledge

For a new analysis, the user maps these out. Neither example below ships in
datatrawl -- they show how two real use cases (an F-statistic DTV detector; a GBO
processed-acquisition energy scan) would each plug their own source/reader/analyzer
into the engine:

| Question | Example: a CHIME DTV detector | Example: a GBO processed-acquisition scan |
|---|---|---|
| Which scope(s)? | `chime.event.baseband.raw`, `chime.scheduled.baseband.raw` | `gbo.acquisition.processed` |
| What does `datatrail ls <scope>` list? | event-like groupings for baseband | larger datasets like `20230512T012608Z_gbo_corr` |
| What is one scan unit? | one `baseband_<event>_<freq_id>.h5` | unknown until a nonempty processed dataset is found |
| What selection exists? | CHIME `freq_id` | likely all files/frequencies |
| Which reader parses it? | `chime-baseband` (shipped) | a reader you write for the processed files |
| Which analyzer runs? | an F-statistic analyzer | an energy-spike analyzer |

## Design rules

1. Do not hide scope choices in instrument geometry.
2. Do not use CHIME baseband freq_id probing for non-baseband products.
3. Keep Datatrail traversal (the source) separate from file-format parsing (the reader) and science (the analyzer).
4. Keep file-format knowledge in readers.
5. Keep science logic in analyzers.
6. Keep corrupt-file quarantine in scan, not survey.
7. Require product-level resume for long analyzers.
