# Reading datatrawl command output

`datatrawl` is intentionally plain-text: output should remain useful in SSH,
`tmux`, `nohup`, and CANFAR logs. This page summarizes what the status lines mean
and what action, if any, a user should take.

## General conventions

- Lines beginning with `error:` need a command or environment change before the
  same operation can succeed.
- Lines beginning with `note:` or `[--]` are informational or skipped checks; they
  do not necessarily block a run.
- `FAIL fetch` means the file was not staged. The file is not marked done; re-run
  the same command to retry.
- `QUARANTINE` means the bytes were staged but could not be read safely. The unit
  is recorded in the quarantine ledger and skipped on later runs unless the ledger
  entry is removed.
- `nothing to do` means the output product already contains every unit for the
  current selection.

## `datatrawl doctor`

`doctor` has two modes.

Without a chosen pipeline it prints the model, ready combinations, and the command
shape to use next:

```bash
datatrawl doctor
```

With a concrete pipeline it checks the requested telescope, source, reader,
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

`READY: core checks passed, but some were SKIPPED` means the local prerequisites
are sound, but Datatrail or another external service could not be validated at
that moment. Do not start a new archive survey until the skipped archive checks
can run.

## `datatrawl survey`

A survey builds or refreshes a small inventory; it does not bulk-download
archive data. Typical lines are:

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

Important terms:

| Term | Meaning |
|---|---|
| `rows written` | Files verified and added to `inventory.jsonl` during this run. |
| `no-data` | Datatrail did not report a usable common path for the event. |
| `accepted-empty` | The event resolved, but no requested files were retrievable after repeated checks. |
| `resolved-but-empty` | The event resolved to no retrievable files in this run and will be retried on the next survey. |
| `incomplete` | Some requested files could not be verified after the retry limit. Verified files are still kept. |

If `inventory.jsonl is EMPTY`, the inventory step found no scan units. Check one
event with Datatrail and one expected CADC URI before scaling up.

## `datatrawl explore`

`explore` summarizes an existing inventory or local directory without staging data:

```text
Available via source 'local' for telescope 'chime'
  files          : 12
  total volume   : 8.4 GB
  freq_ids       : 3 present (614..844)
```

Use the printed `datatrawl scan ... --select ...` hint as a starting point. For a
local directory, make sure the scan command also includes the local source details:
`--source local --source-root <dir> --telescope <name> --reader <reader>`.

## `datatrawl scan`

A scan streams staged files through the reader and analysis plugin, deletes the
staged copy, and checkpoints the small product:

```text
[scan] chime  source=cadc-datatrail  reader=chime-baseband  analyzer=spectrum  (1 product(s))
  [1/1] select=[844]  units=5 -> results/chime/spectrum/844.npz
resume: 2 unit(s) already in results/chime/spectrum/844.npz
5 unit(s) total, 2 done, 0 quarantined, 3 to process -> results/chime/spectrum/844.npz
streaming with 1 download worker(s), <= 1 file(s) on scratch
done: 5/5 units, 3 new this run, 0 failed | {'count': 15, 'files': 5, 'freq_id': 844}
product: results/chime/spectrum/844.npz
```

The recovery rule is simple: after a fetch failure, expired certificate, dropped
terminal, or interrupted session, re-run the identical `scan` command. Completed
units are skipped by product-level resume.

A reader failure while streaming is handled conservatively. The file is
quarantined, but the current analysis state is not checkpointed because it may
include partial updates. Rerun the same command to resume from the last clean
checkpoint and skip the quarantined file.

## Related references

- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) for recovery recipes.
- [`DATATRAIL_BOUNDARY.md`](DATATRAIL_BOUNDARY.md) for Datatrail versus
  `datatrawl` responsibilities.
