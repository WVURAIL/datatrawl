# Long runs, self-healing & troubleshooting

A full pull is *weeks* of data, and nothing about that is fragile: datatrawl
streams one file at a time, checkpoints continuously, and resumes where it left
off. When something interrupts a run, the answer is almost always **re-run the
same command**.

## How self-healing works

- **One file at a time.** The engine stages a file, reduces it, deletes it, moves
  on -- disk never holds more than ~one file. Your archive data is never touched.
- **One product per freq_id.** A multi-freq_id `--select` fans out to independent
  products (`614.npz`, `706.npz`); each checkpoints, resumes, and fails on its own.
- **Atomic checkpoints.** Every `--checkpoint-every` files (default 50) each
  product is rewritten via temp file + atomic rename, never left half-written.
- **Resume by re-running.** On restart each product reloads, reports which files
  it already holds, and does only the rest. Failed files aren't marked done, so
  they retry automatically.

> **Golden rule:** crash, disconnect, expired cert, killed session -- **re-run the
> identical `scan` command.** It resumes from the last checkpoint, never
> double-counts, never corrupts. The only work lost is the few files since the last
> checkpoint of the in-flight freq_id; lower `--checkpoint-every` to shrink that.

## Symptom -> fix

### `datatrail` server not responding (survey or doctor)
datatrawl reaches Datatrail through its Python API; a transient "Datatrail Server at
CHIME is not responding" is the central server being briefly down, not a config or auth
problem. `doctor` reports it as a non-fatal `[--] datatrail scope(s) not validated`
(skipped, not failed). Don't start a `survey` while it persists -- with no listing,
survey would walk an empty inventory. Re-run `datatrail ls` (or `doctor`) until it
returns, then survey/scan as normal.

### CADC cert expired mid-run
Expected on long pulls -- fetches fail with auth errors after ~10 days. Refresh and
re-run; resume skips what's done:
```bash
cadc-get-cert -u <your_cadc_username>
datatrawl scan ... --select 614,706
```
Request a longer cert for campaigns (`--days-valid 30`). Per-freq_id products mean
a lapse only costs the freq_id in flight.

### CANFAR session expired (~4 days) or shut down
Start a fresh session, re-run the same scan. Run under `tmux`/`nohup` so a
disconnect alone doesn't stop it -- but even a hard kill is fine, resume recovers.

### GPU session expired / you need the GPU path
Relaunch your CANFAR GPU session (via the Science Portal or the `canfar` client) and
re-run the same scan -- resume recovers either way.

### Terminal closed / SSH dropped
A foreground scan dies with the terminal. Run it detached (resume recovers either
way):
```bash
nohup datatrawl scan ... --select 614,706 > scan.log 2>&1 &
```
or inside `tmux new -s trawl` (detach `Ctrl-b d`, reattach `tmux attach -t trawl`).

### "No space left on device"
Steady-state disk is ~one file. Check `--tmp-dir` points at fast node-local scratch
(`/scratch/...`), not `/arc`. A hard kill can leave one staged file in `--tmp-dir`;
it's scratch, safe to delete (products live under `results/`).

### Some files failed to fetch
The source retries each a few times with backoff; persistent failures are logged,
counted, and skipped -- not marked done, so re-running retries them. A nonzero exit
means "some fetches failed"; re-run to sweep them up.

### A file won't parse (quarantine)
A file that downloads but won't parse (corrupt/truncated HDF5) is deterministically
bad, so re-fetching won't help. datatrawl quarantines it to
`results/<telescope>/quarantine.jsonl` and excludes it from every future run, so
one bad file can't stall a pull (the scan still exits 0). Review with `cat`;
re-admit by deleting its line and re-running; or `--no-quarantine` to treat
unreadable files as hard failures.

### Are duplicates a problem?
No -- they're ruled out at three layers: `survey` dedups by physical `cadc:` URI,
the source dedups again while enumerating, and resume skips anything already in a
product. A file is fetched and reduced at most once even if it appears in several
scopes.

### `error: <product> was built with <param>=... but this run uses ...`
You changed an analyzer parameter (`--set bracket_hz=...`) on an existing product;
the analyzer won't mix two windows into one. Write the new run to a fresh `--out`.

### New data landed since you surveyed
Re-run `survey` to refresh the inventory, then scan -- files already in your
products are skipped, only new ones are pulled.

### `scan` prints "nothing to do"
That product already holds every file for the selection. To rebuild, delete the
`.npz` (or use a new `--out`).

### Watching progress
A header prints as each freq_id starts; single-freq_id scans print per-file
progress every 25 files. For multi-freq_id, watch `results/<telescope>/spectrum/`
fill, or compare a product's `files` count against `datatrawl explore --inventory <inv>`.

## Recipe for a weeks-long pull

```bash
# 1. fresh, long-lived cert
cadc-get-cert --days-valid 30 -u <your_cadc_username>

# 2. build the inventory once, then see what's in it
datatrawl survey  --telescope chime --freq-ids 614,706 --name chime-614-706
datatrawl explore --inventory data/chime-614-706/inventory.jsonl

# 3. run detached, logging, on node-local scratch (scan reads
#    telescope/source/reader from the inventory meta -> just --analyzer + --select)
tmux new -s trawl
datatrawl scan --inventory data/chime-614-706/inventory.jsonl \
    --analyzer spectrum --select 614,706 \
    --tmp-dir /scratch/trawl --checkpoint-every 25 \
    2>&1 | tee -a /arc/projects/<proj>/trawl.log
#   detach Ctrl-b d ; reattach: tmux attach -t trawl
```

Then keep it alive: refresh the cert as it nears expiry and re-run; relaunch the
CANFAR/GPU session if it expires and re-run. Per-freq_id products accumulate under
`results/`, and your downstream step consumes them as they complete.
