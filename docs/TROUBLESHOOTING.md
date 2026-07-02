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
Steady-state disk is ~one file. For long runs, point `--tmp-dir` at fast
node-local scratch (`/scratch/...`), not `/arc`. Without `--tmp-dir`, datatrawl
creates a unique directory under `DATATRAWL_TMPDIR`, writable `/scratch`, or the
OS temp directory and removes it at exit. A hard kill can leave that one
invocation directory behind; it is scratch and safe to delete (products live
under `results/`).

### Some files failed to fetch
The source retries each a few times with backoff; persistent failures are logged,
counted, and skipped -- not marked done, so re-running retries them. A nonzero exit
means "some fetches failed"; re-run to sweep them up.

### A file won't parse (quarantine)
A file that downloads but won't parse (corrupt/truncated HDF5) is deterministically
bad, so re-fetching usually won't help. datatrawl records it in a
source/reader-scoped ledger under
`results/<telescope>/quarantine/<source>--<reader>.jsonl` and excludes that stable
unit identity from future runs. Files with the same basename but different source
identities remain independent. A probe failure occurs before reduction and can
be skipped immediately. A streaming read
failure may happen after the analyzer has accumulated partial in-memory state, so
datatrawl records the quarantine and aborts without checkpointing; rerun the same
command to resume from the last clean checkpoint and skip that file.

Review the ledger with `cat`. Re-admit a file by deleting its line and rerunning,
or use `--no-quarantine` to treat unreadable files as hard failures.

### An analyzer raises an exception
An unexpected exception from an analyzer is a run-level error, not evidence that
the input file is corrupt. The scan stops, the file is not added to the quarantine
ledger, and the in-memory changes since the last checkpoint are not saved. Fix the
analyzer and rerun the same command; resume starts from the last completed checkpoint.

### Are duplicates a problem?
No -- they're ruled out at three layers: `survey` dedups by physical `cadc:` URI,
the source dedups again while enumerating, and resume skips anything already in a
product. A file is fetched and reduced at most once even if it appears in several
scopes.

### `error: <product> was built with <param>=... but this run uses ...`
You changed an analyzer parameter (`--set bracket_hz=...`) or an engine-level
invariant on an existing product. A common case is trying to continue a bounded
`--max-frames-per-file` smoke-test product with an uncapped run. The analyzer will
not mix incompatible runs; write the new run to a fresh `--out` (or remove the
smoke-test product if it is no longer needed).

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

## Warning: entry-point plugin failed to load (No module named ...)

An installed package advertises a `datatrawl.plugins` entry point whose module
no longer exists. Entry points are recorded in the package's install metadata
at `pip install` time and are **not** refreshed by `git pull` in an editable
install, so this typically means the providing package (e.g. `pilot-proxy`)
was updated after it was installed. Fix by refreshing that package's
metadata:

```bash
pip install -e path/to/the-providing-repo
```

datatrawl continues without the broken plugin either way; the warning only
means that one plugin is unavailable until the metadata is refreshed.
