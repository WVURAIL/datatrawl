"""
datatrawl.interfaces -- the three swappable seams of the streaming engine.

The engine itself (pipeline.py) is fixed: for every *unit* in a discovered
inventory it does

    fetch (stage one file)  ->  read (file -> frames)  ->  analyze (frames -> product)
                            ->  delete the local file   ->  checkpoint

Everything telescope- or science-specific lives behind one of three interfaces
defined here, so adding a new use is implementing a plugin, not editing the
engine:

  DataSource   WHERE the data is and how to enumerate/stage it.
               (the reference one is CADC + the CHIME/FRB Datatrail archive)

  Reader       WHAT a staged file looks like: turn a path into an iterable of
               arrays plus the per-unit metadata the analyzer needs. The reader
               also owns the ARCHIVE FILE SHAPE -- which files one event
               contributes and what they are named (survey_files) -- so an
               archive survey and a later read share one naming definition.
               (the reference one is CHIME 4+4-bit baseband HDF5)

  Analyzer      the SCIENCE: consume those arrays in a single streaming pass and
               accumulate a product that can be checkpointed and saved.
               (the reference one is the averaged power-spectrum analyzer)

A fourth axis, the *instrument* (band/channelization geometry), is data, not
code -- it is a YAML file under instruments/ and is handled by instruments.py.

Plugins advertise themselves through `describe()` so the `list` / `doctor`
discovery commands can show a newcomer what exists and what is still a stub.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping


# --------------------------------------------------------------------------
# Discovery metadata
# --------------------------------------------------------------------------
READY = "ready"            # implemented and validated for production use
EXPERIMENTAL = "experimental"  # works, but not yet trusted for science
STUB = "stub"             # interface only -- TODOs inside; will raise if run

_STATUS_ORDER = {READY: 0, EXPERIMENTAL: 1, STUB: 2}


class SurveyUnavailableError(RuntimeError):
    """A survey stopped cleanly because an external service stayed unavailable.

    Sources raise this after preserving their resumable state. The CLI turns it
    into a nonzero exit without presenting the partial survey as complete.
    """


@dataclass(frozen=True)
class PluginInfo:
    """One row in the discovery tables (`list` / `doctor`)."""
    name: str
    kind: str                      # "source" | "reader" | "analyzer"
    summary: str
    status: str = READY            # READY | EXPERIMENTAL | STUB
    instruments: tuple[str, ...] = ()   # telescopes this plugin is known to fit ("*" = any)
    produces: str = ""             # e.g. "<freq_id>.npz", "n2_<scope>.npz"
    requires: tuple[str, ...] = () # human-readable prerequisites (env, creds, deps)
    notes: str = ""
    # Sources only: True if this source pulls from the CADC archive, so `doctor`'s
    # ready-combos want the telescope to declare a default baseband `scopes` list
    # in YAML (a geometry-only telescope still works if you pass --scope). A "local"
    # source leaves this False, so a geometry-only telescope is usable with it even
    # before any archive scope is configured.
    needs_archive_config: bool = False

    @property
    def status_rank(self) -> int:
        return _STATUS_ORDER.get(self.status, 99)


# --------------------------------------------------------------------------
# A "unit" of work and the streaming context
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Unit:
    """One stage-able item (typically one file) the engine will process.

    `key`   stable identity used for resume (e.g. the cadc: URI). A source must
            avoid emitting duplicate logical units within one enumeration.
    `name`  local filename to stage it under.
    `meta`  source-specific fields a reader/analyzer may need (event id, freq_id,
            obs date, size, ...). Opaque to the engine.
    """
    key: str
    name: str
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RunContext:
    """Shared, read-only-ish context threaded to reader + analyzer for a run."""
    instrument: Any                      # datatrawl.instruments.Instrument
    selection: Any = None                # what the user asked to process (freq_ids,
                                         # events, scope, ...) -- sources parse it
                                         # via plugins/sources/_selection.py
    options: Mapping[str, Any] = field(default_factory=dict)
    reader: Any = None                   # the run's Reader instance, when the caller
                                         # has resolved one. survey() consults it for
                                         # the archive file shape (Reader.survey_files)
                                         # so survey and read share one definition of
                                         # what a unit's file is named.


# --------------------------------------------------------------------------
# DataSource: discovery + staging
# --------------------------------------------------------------------------
class DataSource:
    """Where the data lives and how to enumerate/stage it.

    Two responsibilities, deliberately split so the cheap discovery step can be
    cached and re-run without ever touching bulk data:

      enumerate()  -> iterable of Unit, the inventory for a given selection.
      fetch(unit)  -> stage one unit to a local path; return (ok, error).

    Implementations should make `fetch` retry transient failures and must never
    require more than one staged file to exist at a time -- the engine deletes
    each file right after it is analyzed.

    Thread-safety: with the default engine settings (download_workers=1) fetch is
    called serially, in enumerate() order. With multiple workers and multiple
    staging slots, the engine may call fetch() on a single source instance from
    several threads at once. A parallel-capable source must therefore keep no
    mutable per-call state on self (give each thread its own client, e.g. via
    threading.local, or guard a shared one with a lock).
    """
    info: PluginInfo

    def enumerate(self, ctx: RunContext) -> Iterable[Unit]:
        raise NotImplementedError

    def fetch(self, unit: Unit, dest: str) -> tuple[bool, str]:
        raise NotImplementedError

    # Optional: a one-shot survey that writes a persistent inventory file.
    # Sources whose enumerate() is expensive (network listing) override this so
    # `survey` can cache to disk; cheap sources can leave it as enumerate().
    def survey(self, ctx: RunContext, out_dir: str) -> str:
        raise NotImplementedError(
            f"{self.info.name}: survey-to-disk not implemented; this source "
            f"enumerates on demand.")

    # Optional self-check for `doctor`. Return (ok, problems) -- problems make
    # doctor report NOT READY -- or (ok, problems, notes) to also surface
    # non-fatal "couldn't check" caveats, which doctor renders as a visible [--]
    # skipped line without failing readiness.
    def preflight(self, ctx: RunContext) -> tuple[bool, list[str]]:
        return True, []


# --------------------------------------------------------------------------
# Reader: file -> arrays + metadata
# --------------------------------------------------------------------------
class Reader:
    """Turn one staged file into the arrays an analyzer consumes.

    `probe(path)`            -> dict of per-file metadata (e.g. channel center freq,
                                shape, sample rate) WITHOUT reading bulk data.
    `iter_arrays(path, ctx)` -> yield numpy arrays (the "frames") in streaming order.

    The reader owns the on-disk format knowledge (dataset names, dtype packing,
    attribute conventions). A different file format = a different reader; the
    engine and analyzer do not change.
    """
    info: PluginInfo

    def probe(self, path: str) -> Mapping[str, Any]:
        raise NotImplementedError

    def iter_arrays(self, path: str, ctx: RunContext) -> Iterator:
        raise NotImplementedError

    # -- archive file shape (optional) ------------------------------------
    # An archive survey needs to know, for one event, WHICH files this
    # reader's product contributes and what they are called. That is format
    # knowledge, so it lives on the reader -- the same class that will later
    # open those files -- and survey + read can never drift apart on naming.
    # (This used to be hard-coded as the baseband shape inside the CADC
    # source; a reader that only ever scans pre-listed local files can leave
    # both methods untouched.)
    def survey_files(self, event, common_path, selection,
                     ctx: RunContext) -> Iterable[tuple]:
        """Yield (filename, fields) for every candidate file of one event.

        `filename` is relative to the event's archive common path (it may
        contain a sub-path). `fields` is a mapping of per-file inventory
        columns this format defines -- e.g. the baseband reader yields
        ({"freq_id": ch}) per channel; a per-event calibration product might
        yield a single file with no fields at all. `selection` is whatever
        per-survey spec the source resolved (the baseband survey passes the
        freq_id list); a shape that is not selected that way ignores it.
        Everything yielded here lands verbatim in the inventory row, and the
        row's `name` is what enumerate/fetch later stage -- no re-derivation.
        """
        raise NotImplementedError(
            f"reader {getattr(self.info, 'name', type(self).__name__)!r} does "
            f"not declare an archive file shape (survey_files); an archive "
            f"survey needs a reader that does. See docs/ADDING_A_READER.md.")

    def annotate_row(self, row: dict, instrument) -> None:
        """Optionally enrich one verified inventory row in place.

        Called by survey after the file's size is known, with the run's
        instrument (which may be None). The baseband reader adds freq_mhz and
        n_frames here; the default adds nothing.
        """
        return None

    def preflight(self, ctx: RunContext) -> tuple[bool, list[str]]:
        return True, []


# --------------------------------------------------------------------------
# Analyzer: streaming accumulation -> saveable product
# --------------------------------------------------------------------------
class Analyzer:
    """The science. Accumulate a product over a stream of arrays, in one pass.

    The analyzer OWNS its product file (the saved .npz / .h5 / ...): it writes it,
    re-loads it on resume, and reports which units are already in it. Owning the
    file is what lets an analyzer keep a specific, downstream-readable product
    schema (e.g. the spectrum analyzer writes a self-describing <freq_id>.npz a
    downstream tool can read directly) instead of being wrapped in an opaque
    engine checkpoint.

    Lifecycle, driven by the engine:

        resume(path, ctx)          -> bool : load an existing product if present
                                             AND compatible; True if it resumed
        processed_keys()           -> set  : Unit.key values already in the product
        begin(ctx, first_meta)             : once, when the first new file is read
        consume_file(arrays, meta) -> n    : per file; update accumulators
        save(path)                         : persist the product, provenance, and
                                             processed keys for recovery
        summary()                  -> dict : small human-readable status line

    Keeping all accumulator writes on the engine's main thread (the engine only
    parallelises fetch) means an analyzer needs no locking.

    Ordering: set `requires_in_order = True` if the product depends on the order
    files are consumed -- e.g. a running/trailing statistic, a CFAR baseline, or
    anything that is not a commutative accumulation. The engine then refuses the
    settings (`--download-workers`/`--max-staged-files` > 1) that relax the
    source-order contract, so such an analyzer cannot silently produce an
    order-dependent result. Leave it False (the default) for a commutative
    accumulation like a summed PSD, which is correct at any worker count.
    """
    info: PluginInfo
    requires_in_order: bool = False

    def resume(self, path: str, ctx: RunContext) -> bool:
        """Load an existing product to continue it. Return False if none/incompatible.

        Must raise (not silently continue) if `path` exists but was built with
        incompatible parameters, so two runs can never be mixed into one product.
        """
        return False

    def processed_keys(self) -> set:
        """Unit.key values already accumulated (for resume skip)."""
        return set()

    def begin(self, ctx: RunContext, first_meta: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def resolve_selection(self, ctx: RunContext, spec: Any) -> Any:
        """Interpret a user `--select` spec for this analysis (optional).

        Default is identity. The spectrum analyzer overrides this to interpret
        `--select` as explicit freq_ids (`844`, `614,706`, `506-552`); other
        analyses give it their own meaning (a scope, a date range, a feed
        list, ...).
        """
        return spec

    def plan_runs(self, ctx: RunContext, spec: Any) -> list:
        """Split a selection into INDEPENDENT runs, each its own product.

        Returns a list of sub-selections; the engine runs once per sub-selection
        with a FRESH analyzer instance, so each gets its own resumable product.

        Default: one run over the whole resolved selection. A per-item analysis
        overrides this -- e.g. the spectrum analyzer returns one sub-selection per
        freq_id, so `--select 614,706` becomes two independent <freq_id>.npz
        products that resume (and fail) independently. Returning a single-item
        list `[freq_id]` makes the engine name that product `<freq_id>.npz`.
        """
        return [self.resolve_selection(ctx, spec)]

    def consume_file(self, arrays: Iterable, meta: Mapping[str, Any]) -> int:
        """Consume one file's arrays; return number of frames accumulated.

        Ordering: with the default engine settings the files arrive in source
        (enumerate) order. If a user raises --download-workers or
        --max-staged-files above 1, that ordering is no longer part of the public
        contract. An analyzer that depends on input order (rather than a
        commutative accumulation like a summed PSD) must use the defaults.
        """
        raise NotImplementedError

    def save(self, path: str) -> None:
        raise NotImplementedError

    def summary(self) -> Mapping[str, Any]:
        return {}

    def preflight(self, ctx: RunContext) -> tuple[bool, list[str]]:
        return True, []
