# Reading datatrawl command output

We keep `datatrawl` output in plain text because most long runs are watched over
SSH, in `tmux`, through `nohup`, or from a CANFAR log. This page is the short
version of what each status line means and what to do next.

## General conventions

- `error:` means the command could not complete as requested. Read the message,
  correct the command or environment, and rerun it.
- `note:` is informational. `[--]` means a check could not be completed. Neither
  marker, by itself, says that the run is unusable.
- `FAIL fetch` means the file was not staged. The file is not marked done; re-run
  the same command to retry.
- `QUARANTINE` means the bytes were staged, but the reader could not use them.
  The unit identity is written to the quarantine ledger and excluded on later
  runs until that ledger entry is removed.
- `nothing to do` means that no processable units remain. Each selected unit is
  either already recorded in the product or excluded by the quarantine ledger.

## `datatrawl doctor`

`doctor` has two useful modes.

With no choices, it prints the run model, the registered combinations that fit
together, and the command shape to use next:

```bash
datatrawl doctor
```

With a concrete pipeline, it checks the requested telescope, source, reader,
and analyzer:

```bash
datatrawl doctor \
  --telescope chime \
  --source cadc-datatrail \
  --reader chime-baseband \
  --analyzer spectrum
```

Markers have fixed meanings:

| Marker | Meaning | Action |
|---|---|---|
| `[OK]` | The check passed. | None. |
| `[ ]` | A required check failed. | Apply the suggested fix, then rerun `doctor`. |
| `[--]` | The check could not be completed, but the failure is not conclusive. | Rerun when the external service is reachable. |

`READY: core checks passed, but some were SKIPPED` means the checks that ran
passed, but an external check could not be completed. Before an archive survey,
we rerun `doctor` when Datatrail is reachable rather than committing to a long
walk. The skipped marker does not identify the cause by itself; service,
authentication, and configuration failures can look similar from this layer.

## `datatrawl survey`

A survey builds or refreshes a small inventory. It queries archive metadata but
does not bulk-download the baseband files. A typical run looks like this:

```text
[survey] chime via cadc-datatrail -> data/chime-spectrum-844
[survey] scopes=['chime.event.baseband.raw'] shape=chime-baseband freq_ids=1 (844..844) -> data/chime-spectrum-844/inventory.jsonl
to survey: 80 events
resume: 12 events already done
[15/80] <event>: 1/1 files
survey: 5 events this run -- 5 rows written, 0 no-data, 0 accepted-empty, 0 resolved-but-empty (retry next run), 0 incomplete
survey wrote data/chime-spectrum-844/inventory.jsonl
  inventory: data/chime-spectrum-844/inventory.jsonl
  meta: data/chime-spectrum-844/inventory.meta.json
```

The accounting terms are deliberately separate:

| Term | Meaning |
|---|---|
| `rows written` | Files verified and added to `inventory.jsonl` during this run. |
| `no-data` | Datatrail did not report a usable common path for the event. |
| `accepted-empty` | The event resolved, but no requested files remained retrievable after the survey's repeated checks. |
| `resolved-but-empty` | The event resolved to no retrievable files in this run and will be retried on the next survey. |
| `incomplete` | Some requested files could not be verified after the retry limit. Verified files are still kept. |

If `inventory.jsonl is EMPTY`, the survey has no scan units to hand to the
engine. Check one event in Datatrail and one expected CADC URI before increasing
the survey size. The warning is an observation, not a diagnosis: the files may
be absent, below the survey size floor, inaccessible, or addressed incorrectly.

## `datatrawl explore`

For `cadc-datatrail`, `explore` reads an inventory, so run `survey` first or
pass `--name` / `--inventory`. For `local`, it enumerates the directory named by
`--source-root`.

It summarizes the rows without staging their bulk data:

```text
Available via source 'local' for telescope 'chime'
  files          : 12
  total volume   : 8.4 GB
  freq_ids       : 3 present (614..844)
```

Use the printed `datatrawl scan ... --select ...` hint as a starting point. A
local directory has no inventory sidecar from which to recover the other run
choices, so include the local source details:
`--source local --source-root <dir> --telescope <name> --reader <reader>`.

## `datatrawl scan`

A scan stages each unit, passes it through the reader and analyzer, deletes the
staged copy, and checkpoints the analyzer's product:

```text
[scan] chime  source=cadc-datatrail  reader=chime-baseband  analyzer=spectrum  (1 product(s))
  [1/1] select=[844]  units=5 -> results/chime/spectrum/844.npz
resume: 2 unit(s) already in results/chime/spectrum/844.npz
5 unit(s) total, 2 done, 0 quarantined, 3 to process -> results/chime/spectrum/844.npz
streaming with 1 download worker(s), <= 1 file(s) on scratch
done: 5/5 units, 3 new this run, 0 failed | {'count': 15, 'files': 5, 'freq_id': 844}
product: results/chime/spectrum/844.npz
```

The normal recovery step is to rerun the identical `scan` command. A compatible
product reports its committed unit keys, and the engine skips those keys. Fetch
failures were never committed, so they are attempted again.

A reader failure while streaming is handled conservatively. The unit is added
to the quarantine ledger, but the current in-memory analyzer state is not saved
because it may include a partial update. Rerun the same command to load the last
saved product and exclude the quarantined unit.

These recovery guarantees depend on the analyzer contract. The built-in
`spectrum` analyzer records unit keys, validates its resume parameters, and
writes atomically. An external analyzer must implement the same obligations for
its own product format.

## Related references

- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) for recovery recipes.
- [`DATATRAIL_BOUNDARY.md`](DATATRAIL_BOUNDARY.md) for Datatrail versus
  `datatrawl` responsibilities.
