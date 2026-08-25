# Long runs, recovery, and troubleshooting

A large archive scan can outlive a terminal, a certificate, or a compute
session. `datatrawl` handles that by working one primary file at a time and by
letting the analyzer checkpoint its product. In most interruption cases, the
first recovery step is the same: rerun the same command.

## What recovery actually guarantees

- **Bounded staging.** With the defaults, at most one file is being staged or
  held on scratch. Raising `--max-staged-files` permits bounded prefetching up
  to that count. The engine deletes each staged path after the unit succeeds or
  fails once no source call is writing it; the retained-scratch diagnostic below
  is the safe exception. It does not delete the archive object or local source.
- **Analyzer-defined products.** The built-in `spectrum` analyzer turns a
  multi-freq_id selection into one product per freq_id. For example,
  `--select 614,706` produces `614.npz` and `706.npz`. A different analyzer may
  make a different `plan_runs()` split.
- **Checkpointed progress.** The engine calls `save()` every
  `--checkpoint-every` successfully consumed files (default 50). If at least
  one readable file started the analyzer, it also calls `save()` at the normal
  end of the run. `AccumulatingAnalyzer` and the built-in `spectrum` analyzer
  write through a temporary file and atomic replace. A custom save format owns
  the equivalent safety itself.
- **Resume by committed unit key.** A compatible analyzer product reports the
  unit keys it has already committed. The engine skips those keys on the next
  run. A failed fetch is not committed and is therefore eligible to retry.

> **Normal recovery rule:** after a disconnect, expired credential, stopped
> session, or ordinary process interruption, rerun the identical `scan`
> command. With an analyzer that follows the resume contract, the run restarts
> from its last saved product. Work performed after that checkpoint may be
> repeated.

These guarantees hold only for analyzers that implement the resume contract.
`AccumulatingAnalyzer` supplies atomic writes, committed-key bookkeeping, and a
fail-closed resume manifest covering analyzer identity, instrument geometry,
selection, and analyzer-declared parameters. Custom save/resume implementations
must provide equivalent checks; the built-in `spectrum` analyzer is the
reference.

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
error unless an active writer triggers the retained-scratch diagnostic below.
A hard kill can also leave it behind; the staged paths are scratch and the
products remain under `results/`.

An explicit `--tmp-dir` is used as-is. Give concurrent scans different
directories, and do not point it at a directory containing files you need to
preserve.

### `source.fetch() is still in progress. Scratch was retained at ...`

The scan failed while a downloader was still inside the source's `fetch()` call. The
engine requested cancellation and waited for a bounded interval, but it cannot safely
delete a directory while that call may still be writing there. This is why even an
automatically created scratch directory is retained and its exact path is printed.

Do not remove the reported path while the scan process or fetch is active. Wait for the
operation to return, or end the process, then confirm that no writer is using the path.
Inspect and remove the leftover files and that one scan-specific directory manually; do
not broaden cleanup to the scratch root. Rerun the same scan afterward so product resume
can retry anything not present in the last checkpoint.

A custom source should prevent this state from lasting indefinitely by setting bounded
connect/read/overall network timeouts in `fetch()`. The source authoring contract and an
example are in [`ADDING_A_SOURCE.md`](ADDING_A_SOURCE.md).

### Some files failed to fetch

The shipped CADC source retries a fetch with bounded backoff. If all attempts
fail, the unit is counted as failed and is not committed to the product. The
scan finishes with a nonzero exit status, and rerunning attempts that unit
again. A custom source controls its own retry policy but has the same
`fetch -> (bool, str)` contract with the engine. It must return `(False, detail)`
for an expected retryable/file failure. An exception or malformed return is a
run-level source error and aborts instead of being silently counted as one
failed file.

### A reader reports an unreadable staged file

Only a reader's explicit `datatrawl.interfaces.UnreadableUnitError` classifies
a deterministic file/header/schema problem as eligible for quarantine. With
quarantine enabled, that disposition is written to the source/reader ledger:

```text
results/<telescope>/quarantine/<source>--<reader>.jsonl
```

The ledger stores a stable unit identity, so two units with the same basename
can remain distinct. An `UnreadableUnitError` from `probe()` occurs before the
analyzer consumes data; the engine records it and continues. The same exception
while the reader is yielding arrays has a different boundary: the analyzer may
already contain a partial in-memory update. The engine records the quarantine
and aborts without saving that state. Rerun to load the last saved checkpoint
and exclude the quarantined unit.

Any other reader exception is a run-level failure and does not quarantine the
file. This is intentional: a missing dependency or reader bug is not evidence
that the input is bad. Fix the environment or reader and rerun.

Quarantine means "do not retry automatically," not "the archive copy is proven
permanently corrupt." Review the JSONL record. To test the unit again, stop all
scans using that ledger, remove its line, and rerun. `--no-quarantine` disables
the ledger and treats reader failures as run failures instead.

Several analyzer products can run concurrently while sharing that source/reader
ledger. DataTrawl serializes each ledger read and append with the durable
sidecar `.<ledger-name>.datatrawl.lock`, and rechecks the logical key while
holding it so two active scans record one disposition rather than duplicate or
interleaved JSON. The lock is held only for the small ledger operation, not for
the scan, so products remain independent. A unit newly quarantined after
another scan took its run-start snapshot may still be attempted once by that
already-running scan; subsequent scans exclude it.

Do not delete the sidecar: the operating-system lock is released when the
process exits, while retaining the inode prevents two cooperating processes
from locking different files. These are advisory locks. Every writer must use
DataTrawl, and the shared filesystem must correctly honor POSIX `flock` (or
Windows byte-range locks). Some network-filesystem deployments disable or
misconfigure advisory locking; DataTrawl reports a lock error when the system
call rejects it, but cannot detect a server that falsely claims success. On
such a deployment, point `--quarantine` at a shared filesystem with working
locks, use separate per-job ledgers and reconcile them offline, or disable the
ledger with `--no-quarantine`.

### An analyzer raises an exception

An analyzer exception is a run-level failure, not evidence that the input is
bad. The engine stops, does not add the unit to quarantine, and does not save
the current in-memory changes. Fix the analyzer and rerun from the last saved
product.

### Are duplicate inventory rows a problem?

The answer depends on the unit identity. The shipped `cadc-datatrail` source
uses a source-namespaced logical key built from the inventory row's
`(scope, event, name)`. It collapses rows with that same logical identity during
one enumeration, and product resume skips those keys after they are committed.
The physical CADC URI is kept separately as fetch metadata: relocating the same
logical object does not make it new work or bypass quarantine. This behavior is
exercised by `tests/test_cadc_offline.py`.

The survey file itself is not globally deduplicated, and the generic engine does
not promise to identify equivalent data for an arbitrary custom source. For the
CADC source, changing scope, event, or relative name creates a different logical
unit even if two rows happen to fetch identical bytes. If cross-scope
equivalence matters to the science, define and audit that identity in the
source rather than assuming that physical location or content implies logical
equivalence.

### `error: <product> was built with <param>=... but this run uses ...`

The existing product and current command disagree on a parameter that the
analyzer treats as part of the product definition. A common example is trying
to continue a `--max-frames-per-file` smoke-test product with an uncapped run.
Use a fresh `--out`, or remove the smoke product if it is no longer needed.

For `AccumulatingAnalyzer` subclasses, the exact analyzer-specific parameters
come from `resume_parameters(ctx)`; its default conservatively includes all run
options. A custom `resume()` owns the equivalent validation.

### `existing product has no resume manifest`

The product predates the fail-closed `AccumulatingAnalyzer` format, so the base
class cannot establish that it matches the current analyzer and run. Keep the
old file as provenance and write the new run to a fresh `--out`. If preserving
the accumulated state is essential, write and test a one-off migration that
knows the old producer and every parameter used; do not add a general fallback
that guesses compatibility.

### New data landed after the survey

To discover newly registered events, rerun the original `survey` command with
`--re-enumerate`, then scan again. Product resume skips unit keys already
committed.

If files were added to an event the survey already completed, use a fresh
inventory name. Completed events are not re-probed in place:

```text
--name <new-inventory-name>
```

`survey_state.sqlite3` is the transactional source of truth.
`inventory.jsonl`, `surveyed_events.txt`, and the other text ledgers are
generated views of that database. Do not hand-edit the views, the database, or
`survey_manifest.json`; partial edits can only make the directory internally
inconsistent.

### `survey configuration does not match the state`

An inventory directory is bound by `survey_manifest.json` to the scopes,
frequency IDs, reader shape, instrument geometry, and shape-changing options
that created it. The requested survey differs, so appending could mix
incompatible rows or skip events under the wrong definition. Keep the existing
directory as provenance and choose a fresh `--name`/output. Survey state created
before manifests is refused for the same reason: compatibility cannot be
proven safely.

### `not in CADC storage` lines during survey

An event prints this when its Common Path resolves but every probed file comes
back as a definitive `cadcinfo` NotFound (or below the active reader's
`minimum_archive_bytes` floor). The
absence is an answer, not an error: outages and hard probe failures take
different paths (`service unreachable`, `INCOMPLETE`). Two outcomes follow.

If the observation date parsed from the Common Path is at least
`--empty-age-days` old (default 30), the event is accepted as empty on first
sighting: replication to CADC is long settled by then, so the bytes aged off
the archive or never landed, and re-checking does not recover them. If the
observation is younger, or the Common Path carries no date, the event is
re-checked once per survey run (`re-checking in case transient (n/3)`) in case
replication is still in flight, then accepted as empty after 3 sightings.

Every acceptance is recorded as one JSON line in `no_files_events.jsonl` next
to the inventory (timestamp, sightings, `obs_date`, `common_path`, and a
`reason` of `aged-out` or `max-attempts`), and the event is marked done. To
re-probe accepted events later (for example after an archive restore), survey
into a fresh inventory name. Do not delete keys from the generated text view or
edit the SQLite state.

For batch jobs that must distinguish a fully resolved survey from a resumable
or policy-omitted one, pass `--strict-completeness`. The survey still writes its
transactional state, generated views, and inventory metadata, but the command
exits nonzero while any event is pending, terminally incomplete, or refused by
the Datatrail contract. Accepted-empty and definitive no-data events remain
successful because they are explicit archive dispositions. Rerunning the same
command reuses the preserved state; contract-refused rows must be corrected and
re-opened as described below.

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
datatrawl explore --name chime-614-706

# 3. run detached, logging, on node-local scratch (scan reads
#    telescope/source/reader from the inventory meta -> just --analyzer + --select)
tmux new -s trawl
datatrawl scan --name chime-614-706 \
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

## Survey stalls on one event: "minoc replica outside the expected ... collection"

Symptom: `datatrawl survey` loops `service unreachable -- waiting Ns` on the
same event with growing backoff, then aborts the whole run at the outage
deadline with a renew-your-certificate message -- while every other event is
fine.

Cause: the event's Datatrail answer was well-formed but violated the
adapter's contract (typically a minoc replica outside the collection(s) in
`plugins/sources/_datatrail.py::_MINOC_COLLECTIONS`). That is deterministic:
retrying cannot change it, and it is not an outage.

Behavior now: the survey commits the event as `refused`, records the reason
-- including the offending replica URI verbatim -- in
`no_files_events.jsonl` (`"reason": "datatrail-contract-refusal"`), prints
one line, and continues. Resume skips refused events.

If the ledgered URI turns out to be a legitimate new CADC collection: widen
`_MINOC_COLLECTIONS` (one tuple entry), re-open the refused rows so they are
re-surveyed --

    sqlite3 <inventory-dir>/survey_state.sqlite3 \
        "DELETE FROM events WHERE status='refused'"

-- and rerun the same survey command.
