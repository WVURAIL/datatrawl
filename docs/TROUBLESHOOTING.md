# Long runs, recovery, and troubleshooting

A large archive scan can outlive a terminal, a certificate, or a compute
session. `datatrawl` handles that by working one primary file at a time and by
letting the analyzer checkpoint its product. In most interruption cases, the
first recovery step is the same: rerun the same command.

## What recovery actually guarantees

- **Bounded staging.** With the defaults, at most one file is being staged or
  held on scratch. Raising `--max-staged-files` permits bounded prefetching up
  to that count. The engine deletes each staged path after the unit succeeds or
  fails; it does not delete the archive object or the local source file.
- **Analyzer-defined products.** The built-in `spectrum` analyzer turns a
  multi-freq_id selection into one product per freq_id. For example,
  `--select 614,706` produces `614.npz` and `706.npz`. A different analyzer may
  make a different `plan_runs()` split.
- **Checkpointed progress.** The engine calls `save()` every
  `--checkpoint-every` successfully consumed files (default 50). If at least
  one readable file started the analyzer, it also calls `save()` at the normal
  end of the run. `AccumulatingAnalyzer` and the built-in `spectrum` analyzer
  write through a temporary file and atomic replace. An external analyzer owns
  its file format and must provide the equivalent safety itself.
- **Resume by committed unit key.** A compatible analyzer product reports the
  unit keys it has already committed. The engine skips those keys on the next
  run. A failed fetch is not committed and is therefore eligible to retry.

> **Normal recovery rule:** after a disconnect, expired credential, stopped
> session, or ordinary process interruption, rerun the identical `scan`
> command. With an analyzer that follows the resume contract, the run restarts
> from its last saved product. Work performed after that checkpoint may be
> repeated.

These guarantees are not magic around an arbitrary plugin. A resumable analyzer
must persist committed unit keys, reject incompatible run parameters, and save
its product safely. `AccumulatingAnalyzer` supplies atomic NPZ replacement and
processed-key restoration. The built-in `spectrum` analyzer is the reference
for validating current run parameters during resume.

## Symptom -> fix

### `datatrail` did not answer during `doctor` or survey

`datatrawl` calls Datatrail through `datatrail ls/ps --json`. A failed call may
come from the service, authentication, site configuration, or the installed CLI
contract; this layer cannot always distinguish those causes from one response.
`doctor` reports an inconclusive scope check as
`[--] datatrail scope(s) not validated`.

Do not start a large event survey from that state. Event enumeration uses the
Datatrail listings, so an unanswered listing can leave the survey with no events
to inspect. Run `datatrail ls` directly, correct any reported authentication or
configuration problem, and rerun `doctor` before surveying. During per-event
verification, a sustained Datatrail/CADC outage causes the survey to exit
nonzero after preserving its partial state.

### CADC certificate expired during a scan

Refresh the certificate and rerun. Units already committed to the product are
skipped; units whose fetch failed are tried again.

```bash
cadc-get-cert -u <your_cadc_username>
datatrawl scan ... --select 614,706
```

If your certificate service supports it, request a lifetime appropriate for the
campaign (`--days-valid 30`). The actual lifetime is set by the issued
certificate, not by `datatrawl`. For a fan-out analyzer, products already saved
remain usable; the active product may repeat work since its last checkpoint.

### CANFAR session ended or was shut down

Start a new session and rerun the same scan. `tmux` or `nohup` protects a process
from a terminal disconnect, but it cannot keep a process alive after the compute
session itself ends. Product-level resume handles the latter case.

### A GPU session ended

Start another compatible GPU session and rerun the scan. If the new image has a
different CUDA environment, run `datatrawl setup-cupy` before continuing. The
analyzer is still responsible for rejecting any product incompatibility.

### Terminal closed or SSH dropped

A foreground process normally receives the terminal's hangup. Run it detached:

```bash
nohup datatrawl scan ... --select 614,706 > scan.log 2>&1 &
```

or start `tmux new -s trawl` (detach with `Ctrl-b d`; reattach with
`tmux attach -t trawl`). If the process did stop, rerun the same command.

### "No space left on device"

At the defaults, allow scratch space for the largest selected file plus product
checkpoint overhead. If `--max-staged-files` is greater than one, allow for up
to that many staged files.

For long runs, use a scan-specific directory on fast node-local scratch, for
example `/scratch/...`, rather than an archival project directory. Without
`--tmp-dir`, `datatrawl` creates a unique directory under `DATATRAWL_TMPDIR`, a
writable `/scratch`, or the operating-system temporary directory. It removes
that automatic directory when the command exits normally or through a handled
error. A hard kill can leave it behind; the staged paths are scratch and the
products remain under `results/`.

An explicit `--tmp-dir` is used as-is. Give concurrent scans different
directories, and do not point it at a directory containing files you need to
preserve.

### Some files failed to fetch

The shipped CADC source retries a fetch with bounded backoff. If all attempts
fail, the unit is counted as failed and is not committed to the product. The
scan finishes with a nonzero exit status, and rerunning attempts that unit
again. A custom source controls its own retry policy but has the same
`fetch -> (ok, error)` contract with the engine.

### A staged file will not parse

With quarantine enabled, a failed reader probe is written to the source/reader
ledger:

```text
results/<telescope>/quarantine/<source>--<reader>.jsonl
```

The ledger stores a stable unit identity, so two units with the same basename
can remain distinct. A probe failure occurs before the analyzer consumes data;
the engine records it and continues. A failure while the reader is yielding
arrays is different: the analyzer may already contain a partial in-memory
update. The engine records the quarantine and aborts without saving that state.
Rerun to load the last saved checkpoint and exclude the quarantined unit.

Quarantine means “do not retry automatically,” not “the archive copy is proven
permanently corrupt.” Review the JSONL record. To test the unit again, remove
its ledger line and rerun. `--no-quarantine` disables the ledger and treats
reader failures as run failures instead.

### An analyzer raises an exception

An analyzer exception is a run-level failure, not evidence that the input is
bad. The engine stops, does not add the unit to quarantine, and does not save
the current in-memory changes. Fix the analyzer and rerun from the last saved
product.

### Are duplicate inventory rows a problem?

The answer depends on the unit identity. The shipped `cadc-datatrail` source
collapses repeated inventory rows that resolve to the same full CADC URI during
one enumeration. On a later run, product resume skips unit keys already saved in
that product. This behavior is exercised by `tests/test_cadc_offline.py`.

The survey file itself is not globally deduplicated, and the generic engine does
not promise to identify equivalent data from different URIs or from an
arbitrary custom source. Two rows with different full URIs are different units,
even if their basenames or contents happen to match. If cross-scope equivalence
matters to the science, define and audit that identity in the source rather than
assuming that every physical file is fetched and analyzed at most once.

### `error: <product> was built with <param>=... but this run uses ...`

The existing product and current command disagree on a parameter that the
analyzer treats as part of the product definition. A common example is trying
to continue a `--max-frames-per-file` smoke-test product with an uncapped run.
Use a fresh `--out`, or remove the smoke product if it is no longer needed.

For external analyzers, the exact protected parameters are whatever that
analyzer stamps and validates in `resume()`.

### New data landed after the survey

To discover newly registered events, rerun the original `survey` command with
`--re-enumerate`, then scan again. Product resume skips unit keys already
committed.

If files were added to an event already listed in `surveyed_events.txt`, use a
fresh inventory name. Completed events are not re-probed in place:

```text
--name <new-inventory-name>
```

### `scan` prints `nothing to do`

No processable units remain for that product. The selected units are either
already represented by committed keys or excluded by the quarantine ledger. To
rebuild the product, use a new `--out` or remove the existing product. To retry
a quarantined unit, remove its ledger entry first.

### Watching progress

A header is printed when each analyzer-planned product starts. With one product,
the engine prints a progress line every 25 newly consumed files. For the
built-in multi-freq_id spectrum run, watch
`results/<telescope>/spectrum/` fill, or compare a product's `files` array with
the corresponding freq_id count from `datatrawl explore --inventory <inv>`.

## Recipe for a long archive pull

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

Refresh the certificate before it expires and rerun after any stopped compute
session. For the built-in spectrum analyzer, each freq_id product accumulates
under `results/<telescope>/spectrum/` and can be used as it completes.

## Warning: entry-point plugin failed to load (`No module named ...`)

An installed package advertises a `datatrawl.plugins` entry point whose target
could not be imported. One common cause is stale install metadata after the
providing repository changed. Refresh that package's editable installation:

```bash
pip install -e path/to/the-providing-repo
```

If the warning remains, import the target module directly to find the underlying
dependency or package error. `datatrawl` continues loading the other plugins;
only the failed entry point is unavailable.
