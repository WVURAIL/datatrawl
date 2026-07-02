"""
Data source: CADC storage + the CHIME/FRB Datatrail archive.

A source for CANFAR archive work, in three halves:

  enumerate()  read an inventory (inventory.jsonl) and yield one Unit per file,
               filtered to the selected freq_ids and/or events (see
               `_selection.py` for the grammar). The inventory is a cheap,
               offline listing, so enumerate itself never touches the network.

  survey()     build that inventory.jsonl: walk the Datatrail scope(s), discover
               every event, and verify each file the reader's archive shape
               (Reader.survey_files) declares for it at CADC -- one HDF5 per
               freq_id for baseband; whatever a different product's reader
               declares for that product. Resumable + incremental.

  fetch()      stage one file with cadcget via a CADC StorageInventoryClient,
               authenticated by a proxy certificate, with bounded retries.

Prerequisites (checked by `doctor`):
  * a valid CADC proxy cert (CADC_CERT or ~/.ssl/cadcproxy.pem)
  * the `cadcdata` / `cadcutils` packages  (for enumerate/fetch/verify)
  * the `datatrail` CLI on PATH            (for survey only -- `[survey]` extra)
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, List

from ...interfaces import (DataSource, RunContext, Unit, PluginInfo, READY,
                           SurveyUnavailableError)
from ...registry import source as _register_source
from ._datatrail import DATATRAIL, Datatrail
from ._selection import parse_freq_ids, parse_selection


# --------------------------------------------------------------------------
# file naming: the reader owns it (Reader.survey_files); this source only
# JOINS a common path and a per-row filename into a CADC URI, and falls back
# to the baseband reader's naming for legacy inventory rows written before
# rows carried an explicit `name`.
# --------------------------------------------------------------------------
def _join_uri(common_path, name) -> str:
    return f"{str(common_path).rstrip('/')}/{str(name).lstrip('/')}"


def _default_shape():
    """The reader whose file shape survey uses when the caller supplied none.

    Kept for compatibility with pre-`--reader` invocations (and direct
    src.survey() calls in tests): the chime-baseband reader IS the naming this
    source hard-coded historically, so falling back to it changes nothing.
    Imported lazily so merely importing this module never pulls the reader
    stack.
    """
    from ..readers.chime_baseband import ChimeBasebandReader
    return ChimeBasebandReader()


def _legacy_row_name(event, freq_id) -> str:
    """Filename for an inventory row that predates the `name` column (always
    baseband-shaped -- that was the only product then). Delegates to the
    reader's naming so there is exactly one definition of it."""
    from ..readers.chime_baseband import baseband_filename
    return baseband_filename(event, freq_id)


def _parse_freq_id_set(sel):
    """Legacy alias: the freq_id grammar now lives in `_selection.parse_freq_ids`
    (shared across sources, alongside the event grammar). Kept so existing
    imports and the survey's `_resolve_freq_ids` keep working unchanged."""
    return parse_freq_ids(sel)


def _default_cert() -> str:
    """The CADC proxy cert datatrawl uses for the (datatrail-free) fetch path.

    CADC_CERT overrides (honoured even if absent -- the user's stated intent, and
    the clean way to point a headless/batch job at a different cert); otherwise the
    standard ~/.ssl/cadcproxy.pem that `cadc-get-cert` writes. preflight() checks
    the resolved path actually exists, so a missing or unrenewed cert is flagged at
    doctor time rather than mid-fetch.
    """
    return os.environ.get("CADC_CERT") or os.path.expanduser("~/.ssl/cadcproxy.pem")


# --------------------------------------------------------------------------
# survey: defaults, geometry, and the Datatrail CLI plumbing
# --------------------------------------------------------------------------
# Final fallback when neither --scope nor the telescope's YAML `scopes` resolve.
# Each station (chime + outriggers kko/gbo/hco) now declares its own baseband
# scope(s) in instruments/*.yaml, so survey defaults to those; this constant only
# applies to a telescope that declares none. (Outrigger-LABELLED chime events are
# still dropped unless --include-outrigger.)
_DEFAULT_SCOPES = ("chime.event.baseband.raw", "chime.scheduled.baseband.raw")

_MIN_VALID_BYTES = 1 << 20

_SOCKET_TIMEOUT = 120        # s: cap any single cadcinfo socket op
_MAX_ATTEMPTS = 3            # per-event verification retries across resumes
_MAX_SERVICE_WAIT = 3600     # s to ride out a service/cert outage before aborting

_DATE_RE = re.compile(r"/raw/(\d{4})/(\d{2})/(\d{2})/")
_OUTRIGGER_RE = re.compile(r"outrigger", re.IGNORECASE)


def _resolve_freq_ids(spec, n_channels: int) -> List[int]:
    """--freq-ids -> a sorted list of freq_id ints. Accepts 'all' (every freq_id in
    the instrument's band, i.e. range(n_channels)), a list, or the same string forms
    as --select."""
    all_ids = list(range(int(n_channels)))
    if spec is None:
        return all_ids
    if isinstance(spec, str) and spec.strip().lower() == "all":
        return all_ids
    freq_ids = _parse_freq_id_set(spec)
    return sorted(freq_ids) if freq_ids else all_ids


# -- recon ("recursive ls"): discover scopes/datasets without enumerating files -
def _match_terms(spec) -> List[str]:
    """--match -> list of lowercased substrings (comma-separated), ANDed."""
    if not spec:
        return []
    return [t.strip().lower() for t in str(spec).split(",") if t.strip()]


def _keep(text: str, terms: List[str]) -> bool:
    low = text.lower()
    return all(t in low for t in terms)


def _recon(named_scopes, match_terms: List[str], out_dir: str) -> str:
    """Recursive `datatrail ls`: list the datasets under each scope, with NO
    event/file enumeration -- the cross-scope survey-of-the-landscape datatrail
    itself can't do in one call. Writes scopes.jsonl ({scope, dataset}) and
    prints a readable map. `named_scopes=None` walks every scope datatrail sees.

    Filtering is name-level only (instrument / type / format as they appear in
    the scope & dataset names); deeper criteria like validity or exact frequency
    coverage need the full survey, which actually reads the files.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scopes = list(named_scopes) if named_scopes else DATATRAIL.list_scopes()
    print(f"[recon] listing datasets across {len(scopes)} scope(s)"
          + (f"; match={match_terms}" if match_terms else ""), flush=True)

    map_path = out / "scopes.jsonl"
    rows = 0
    with open(map_path, "w") as fh:
        for i, s in enumerate(scopes, 1):
            datasets = DATATRAIL.list_datasets(s)
            kept = ([d for d in datasets if _keep(f"{s} {d}", match_terms)]
                    if match_terms else datasets)
            if not kept:
                continue
            print(f"  [{i:>3}/{len(scopes)}] {s}  ({len(kept)} dataset(s))",
                  flush=True)
            for d in kept:
                print(f"        {d}")
                fh.write(json.dumps({"scope": s, "dataset": d}) + "\n")
                rows += 1

    print(f"\n[recon] wrote {map_path}: {rows} (scope, dataset) rows. This is a "
          f"discovery map, not the scan inventory -- pick the scope(s) you want "
          f"and re-run survey without --scopes-only.", flush=True)
    return str(map_path)


def _load_json_file(path: Path, default):
    """Read+parse JSON from `path`; return `default` if the file is missing,
    empty, or corrupt -- e.g. a checkpoint a previous run was killed mid-write.
    """
    try:
        if not path.exists():
            return default
        text = path.read_text().strip()
        return json.loads(text) if text else default
    except (json.JSONDecodeError, ValueError, OSError):
        return default


def _enumerate_events(scopes, include_outrigger, cache_path: Path, re_enumerate):
    """{(scope, event): [labels...]} across all larger-datasets, cached to disk.

    Phase 1: cheap, network-only listing. Cached so a re-run skips straight to
    verification unless --re-enumerate is given.
    """
    if cache_path.exists() and not re_enumerate:
        raw = _load_json_file(cache_path, None)
        if raw:
            cached = {tuple(k.split("|", 1)): v for k, v in raw.items()}
            cached_scopes = sorted({s for (s, _e) in cached})
            if cached_scopes == sorted(scopes):
                return cached
            # The dir was enumerated for different scope(s). Serving the stale list
            # would silently survey the wrong scope, so stop with an actionable error
            # rather than guess.
            raise SystemExit(
                f"inventory directory {cache_path.parent} was already enumerated for "
                f"scope(s) {cached_scopes}, but this run requests {sorted(scopes)}. An "
                f"inventory is tied to its scope(s) -- use a different --name, delete "
                f"{cache_path.parent} to rebuild from scratch, or pass --re-enumerate "
                f"to re-list the requested scope(s) into it.")
        # corrupt/empty cache -> fall through and re-enumerate

    membership: dict = defaultdict(set)
    for scope in scopes:
        datasets = DATATRAIL.list_datasets(scope)
        # One `datatrail ls` per dataset -- a slow walk -- so show progress, but
        # not all N names (use --scopes-only recon to inspect dataset structure).
        print(f"{scope}: walking {len(datasets)} larger-dataset(s)", flush=True)
        step = max(1, len(datasets) // 10)
        for i, ds in enumerate(datasets, 1):
            for ev in DATATRAIL.events_in_dataset(scope, ds):
                membership[(scope, ev)].add(ds)
            if i % step == 0 or i == len(datasets):
                print(f"  ...{i}/{len(datasets)} datasets", flush=True)

    out_map = {f"{s}|{e}": sorted(lbls) for (s, e), lbls in membership.items()}
    cache_path.write_text(json.dumps(out_map))
    n_out = sum(1 for lbls in membership.values()
                if any(_OUTRIGGER_RE.search(x) for x in lbls))
    print(f"\nenumerated {len(membership)} unique events; {n_out} carry an "
          f"outrigger label ({'KEPT' if include_outrigger else 'BLOCKED'})", flush=True)
    return {k: sorted(v) for k, v in membership.items()}


def _commit_decision(n_errored: int, n_total: int, attempts: int,
                     n_records: int | None = None,
                     max_attempts: int = _MAX_ATTEMPTS) -> tuple:
    """(write_records, mark_done, incomplete, made_progress) for one event.

      clean, rows resolved           -> write rows, mark done
      partial, attempts left         -> write nothing, don't mark (clean retry)
      partial, out of attempts       -> accept what verified, mark done, flag
      empty (cp resolved, 0 rows,    -> write nothing, retry across resumes, then
        0 hard errors), retries left     accept-as-empty + mark done -- so a 0-row
      empty, out of attempts            event can never silently read as "clean"

    `n_records` is how many freq_ids verified PRESENT. Zero PRESENT *and* zero
    errored means the event resolved a common path but produced no usable file:
    distinct from a fully-resolved event, and exactly the case that used to be
    written out as a clean 0-row 'done'. Passing `n_records=None` preserves the
    old errored-only contract for any caller that does not supply it.
    `made_progress` = at least one freq_id gave a definitive present/absent
    answer (NotFound counts); drives the outage circuit-breaker.
    """
    if n_errored == 0:
        if n_records == 0:                        # resolved, but nothing present
            if attempts + 1 >= max_attempts:
                return False, True, False, True   # accept-as-empty: mark done
            return False, False, False, True      # retry on the next resume
        return True, True, False, True            # clean, rows to write
    made_progress = n_errored < n_total
    if attempts + 1 >= max_attempts:
        return True, True, True, made_progress
    return False, False, False, made_progress


@_register_source
class CadcDatatrailSource(DataSource):
    info = PluginInfo(
        name="cadc-datatrail",
        kind="source",
        summary="CHIME/FRB baseband on CADC; survey to build, enumerate offline.",
        status=READY,
        instruments=("chime", "kko", "gbo", "hco"),
        requires=("CADC proxy cert", "cadcdata", "cadcutils",
                  "datatrail CLI (survey only)"),
        needs_archive_config=True,
        notes="survey() walks the Datatrail scope(s) to build inventory.jsonl; "
              "enumerate() then reads it offline; fetch() uses cadcget.",
    )

    def __init__(self) -> None:
        # One client per thread: fetch()/cadcinfo() may run concurrently (download
        # workers, or the survey verify pool), and a shared CADC client is not
        # guaranteed safe for concurrent calls.
        self._local = threading.local()

    def _get_client(self):
        cl = getattr(self._local, "client", None)
        if cl is None:
            cl = self._local.client = self._make_client()
        return cl

    def _make_client(self, cert=None):
        from cadcdata import StorageInventoryClient
        from cadcutils import net
        cert = cert or _default_cert()
        subj = (net.Subject(certificate=cert)
                if cert and os.path.exists(cert) else net.Subject())
        return StorageInventoryClient(subj)

    # -- enumerate -----------------------------------------------------------
    def _inventory_path(self, ctx: RunContext) -> str:
        o = ctx.options or {}
        p = o.get("inventory")
        if p:
            return p
        base = o.get("root", ".")
        tel = ctx.instrument.name
        return f"{base}/data/{tel}/inventory.jsonl"

    def enumerate(self, ctx: RunContext) -> Iterable[Unit]:
        path = self._inventory_path(ctx)
        if not os.path.exists(path):
            raise SystemExit(
                f"inventory not found: {path}\n"
                f"Build one with `datatrawl survey` (or pass --inventory <path>).")
        sel = parse_selection(ctx.selection)
        seen, units = set(), []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ch = r.get("freq_id")
                if not sel.wants_freq_id(ch):
                    continue
                if not sel.wants_event(r.get("event")):
                    continue
                # Rows are self-describing since the shape moved onto the
                # reader: survey writes each file's `name`, and enumerate just
                # joins it to the common path -- no naming re-derivation, so
                # survey and read cannot drift. A row WITHOUT `name` predates
                # that (baseband was the only product) and gets the baseband
                # reconstruction.
                name = r.get("name") or _legacy_row_name(r["event"], ch)
                uri = _join_uri(r["common_path"], name)
                if uri in seen:
                    continue
                seen.add(uri)
                # meta: the row, minus the URI ingredients, plus the stable
                # quarantine identity. Shape-specific columns (freq_id for
                # baseband; whatever a calibration shape wrote) ride through
                # untouched -- meta is opaque to the engine and is exactly how
                # an analyzer keys a companion lookup (e.g. gains by event).
                meta = {k: v for k, v in r.items()
                        if k not in ("common_path", "name")}
                if ch is not None:
                    meta["freq_id"] = int(ch)
                meta["size_bytes"] = int(r.get("size_bytes", 0))
                meta["quarantine_key"] = (f"{r['event']}:{ch}" if ch is not None
                                          else f"{r['event']}:{name}")
                units.append(Unit(key=uri, name=name, meta=meta))
        return units

    # -- fetch ---------------------------------------------------------------
    def fetch(self, unit: Unit, dest: str, retries: int = 3, base: float = 4.0):
        client = self._get_client()
        delay, last = base, None
        for k in range(retries + 1):
            try:
                client.cadcget(unit.key, dest=dest)
                if os.path.exists(dest) and os.path.getsize(dest) > 0:
                    return True, ""
                last = "empty file"
            except Exception as exc:                       # noqa: BLE001
                if "NotFound" in type(exc).__name__:
                    return False, "NotFound"
                last = f"{type(exc).__name__}: {exc}"
            if k < retries:
                time.sleep(delay)
                delay *= 2
        return False, str(last)[:200]

    # -- survey ---------------------------------------
    # (The per-product file shape -- which files one event contributes, and
    # their names -- used to live here as `_event_files`. Step 2 of the design
    # moved it onto the reader (Reader.survey_files) so survey and read share
    # one naming definition and cannot drift; survey() below consults
    # ctx.reader, falling back to the chime-baseband reader's shape.)
    def _cadc_size(self, uri, retries: int = 3, base: float = 4.0):
        delay, last = base, None
        for k in range(retries + 1):
            try:
                return self._get_client().cadcinfo(uri).size, None
            except Exception as exc:                       # noqa: BLE001
                if "NotFound" in type(exc).__name__:
                    return None, None                       # definitive: absent
                last = exc
                if k < retries:
                    time.sleep(delay)
                    delay *= 2
        return None, last

    def survey(self, ctx: RunContext, out_dir: str) -> str:
        """Build inventory.jsonl for the selected scope(s) + freq_ids.

        Two phases, resumable and incremental: enumerate the unique events under
        each scope (cached), then for every not-yet-done event resolve its Common
        Path and cadcinfo each requested freq_id, writing verified rows atomically
        per event. Re-running tops up new events without re-surveying.
        """
        o = ctx.options or {}
        scope_opt = o.get("scope")
        named = (tuple(s.strip() for s in
                       (scope_opt.split(",") if isinstance(scope_opt, str) else scope_opt)
                       if str(s).strip())
                 if scope_opt else None)
        if o.get("scopes_only"):                 # recon: recursive `datatrail ls`
            return _recon(named, _match_terms(o.get("match")), out_dir)
        inst_scopes = tuple(getattr(ctx.instrument, "scopes", ()) or ())
        scopes = named or inst_scopes or _DEFAULT_SCOPES
        n_ch = ctx.instrument.n_channels if ctx.instrument is not None else 0
        freq_ids = _resolve_freq_ids(o.get("freq_ids", ctx.selection), n_ch)
        include_outrigger = bool(o.get("include_outrigger", False))
        workers = max(1, int(o.get("workers", 12) or 12))
        max_events = o.get("max_events")
        re_enumerate = bool(o.get("re_enumerate", False))

        socket.setdefaulttimeout(_SOCKET_TIMEOUT)
        # The reader owns the archive file shape (which files one event
        # contributes, and their names -- Reader.survey_files). The CLI resolves
        # the run's reader onto ctx.reader; a caller that supplied none gets the
        # chime-baseband shape, which is byte-for-byte what this source used to
        # hard-code, so pre-existing invocations survey identically.
        shape = ctx.reader if ctx.reader is not None else _default_shape()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        inv_path = out / "inventory.jsonl"
        fid_note = (f" freq_ids={len(freq_ids)} ({freq_ids[0]}..{freq_ids[-1]})"
                    if freq_ids else "")
        print(f"[survey] scopes={list(scopes)}"
              f" shape={getattr(shape.info, 'name', type(shape).__name__)}"
              f"{fid_note} -> {inv_path}", flush=True)

        # ---- phase 1: enumerate the unique events (cached) ----
        membership = _enumerate_events(scopes, include_outrigger,
                                       out / "enum_cache.json", re_enumerate)
        events = sorted(ev for ev, lbls in membership.items()
                        if include_outrigger
                        or not any(_OUTRIGGER_RE.search(x) for x in lbls))
        print(f"to survey: {len(events)} events", flush=True)

        # ---- phase 2: verify each event's freq_ids (resumable) ----
        surveyed_path = out / "surveyed_events.txt"
        attempts_path = out / "attempts.json"
        surveyed = set(surveyed_path.read_text().split()) if surveyed_path.exists() else set()
        attempts = _load_json_file(attempts_path, {})
        print(f"resume: {len(surveyed)} events already done", flush=True)

        inv_f = open(inv_path, "a")
        surveyed_f = open(surveyed_path, "a")
        incomplete_f = open(out / "incomplete_events.txt", "a")
        no_files_f = open(out / "no_files_events.txt", "a")
        pool = ThreadPoolExecutor(max_workers=workers)
        n_new = 0
        # run-level accounting so a 0-row inventory can never read as success
        n_rows = n_no_data = n_incomplete = n_empty_retry = n_empty_accepted = 0

        def mark_done(key):
            surveyed_f.write(key + "\n"); surveyed_f.flush(); surveyed.add(key)
            attempts.pop(key, None)

        def bump(key):
            attempts[key] = attempts.get(key, 0) + 1
            attempts_path.write_text(json.dumps(attempts))

        def verify(scope, ev):
            cp, ps_ok = DATATRAIL.common_path(scope, ev)
            if not ps_ok:
                return "service_down", [], [], 0
            if not cp:
                return "no_data", [], [], 0
            obs_date = (lambda m: f"{m[1]}-{m[2]}-{m[3]}" if m else "unknown")(
                _DATE_RE.search(cp))
            labels = membership[(scope, ev)]
            # The candidate files one event contributes -- (name, fields) pairs
            # from the reader's shape. Baseband yields one per freq_id; a
            # per-event product may yield a single file with its own fields.
            cand = list(shape.survey_files(ev, cp, freq_ids, ctx))

            def probe(item):
                name, fields = item
                size, err = self._cadc_size(_join_uri(cp, name))
                return name, fields, size, err

            records, errored = [], []
            for name, fields, size, err in pool.map(probe, cand):
                if err is not None:
                    errored.append(name)
                elif size is not None and size >= _MIN_VALID_BYTES:
                    # Self-describing row: `name` is what enumerate/fetch will
                    # stage (joined to common_path), and the shape's per-file
                    # fields land verbatim as columns.
                    rec = {
                        "scope": scope, "event": ev, "name": name,
                        "size_bytes": size,
                        "common_path": cp, "obs_date": obs_date, "datasets": labels,
                    }
                    rec.update(fields or {})
                    shape.annotate_row(rec, ctx.instrument)
                    records.append(rec)
            if errored and len(errored) == len(cand):       # all errored -> outage
                return "service_down", records, errored, len(cand)
            return "progress", records, errored, len(cand)

        try:
            for i, (scope, ev) in enumerate(events, 1):
                key = f"{scope}|{ev}"
                if key in surveyed:
                    continue
                if max_events is not None and n_new >= int(max_events):
                    print(f"reached --max-events={max_events}; stopping "
                          f"(resumable).", flush=True)
                    break

                # ride out a transient outage on the SAME event; only a sustained
                # one aborts (the signature of an expired cert, which won't heal).
                backoff, waited = 60, 0
                while True:
                    status, records, errored, n_cand = verify(scope, ev)
                    if status != "service_down":
                        break
                    if waited >= _MAX_SERVICE_WAIT:
                        raise SurveyUnavailableError(
                            f"Datatrail/CADC remained unreachable for {waited}s. "
                            f"Partial survey state was preserved in {out}. Renew "
                            "the certificate with `cadc-get-cert -u <user>` (or "
                            "wait for the service to recover), then rerun the same "
                            "survey command."
                        )
                    print(f"[{i}/{len(events)}] {ev}: service unreachable -- "
                          f"waiting {backoff}s (waited {waited}s)", flush=True)
                    time.sleep(backoff); waited += backoff
                    backoff = min(backoff * 2, 600)

                n_new += 1
                if status == "no_data":
                    n_no_data += 1
                    mark_done(key); continue

                # `empty` = a common path resolved but every requested freq_id
                # came back absent/sub-floor: 0 rows AND 0 hard errors. That is
                # NOT a clean, fully-resolved event -- it is the case that used
                # to be written out as a silent 0-row "done". _commit_decision
                # now keeps such an event un-done (retried across resumes) until
                # _MAX_ATTEMPTS, then accepts it as genuinely empty + records it
                # in no_files_events.txt, so it can neither vanish nor re-probe
                # forever.
                empty = not records and not errored
                write_recs, done, incomplete, _ = _commit_decision(
                    len(errored), n_cand, attempts.get(key, 0),
                    n_records=len(records))
                if write_recs:
                    for rec in records:
                        inv_f.write(json.dumps(rec) + "\n")
                    n_rows += len(records)
                    inv_f.flush()
                if incomplete:
                    incomplete_f.write(
                        f"{key}\tunresolved={','.join(map(str, errored))}\n")
                    incomplete_f.flush()
                    n_incomplete += 1
                if done:
                    mark_done(key)
                    if empty:                  # terminal 0-file event: log it
                        no_files_f.write(key + "\n"); no_files_f.flush()
                        n_empty_accepted += 1
                else:
                    bump(key)
                    if empty:
                        n_empty_retry += 1

                # surface the empty case too -- otherwise it prints nothing and
                # the run looks like it did nothing at all (the original bug's
                # tell: 0 events processed visibly, 0 rows on disk).
                if records or errored or empty or i % 100 == 0:
                    if empty:
                        tag = (" -- not in CADC storage; accepting as empty" if done
                               else f" -- not in CADC storage; re-checking in case "
                               f"transient ({attempts.get(key, 0)}/{_MAX_ATTEMPTS})")
                    else:
                        tag = (f" INCOMPLETE({len(errored)})" if incomplete
                               else f" ({len(errored)} unresolved, retry)"
                               if errored else "")
                    print(f"[{i}/{len(events)}] {ev}: "
                          f"{len(records)}/{n_cand} files{tag}", flush=True)
        finally:
            attempts_path.write_text(json.dumps(attempts))
            pool.shutdown(wait=False)
            inv_f.close(); surveyed_f.close(); incomplete_f.close()
            no_files_f.close()

        # Row-level accounting: the final word on the run, so an empty inventory
        # is impossible to mistake for success (the failure mode that hid behind
        # "survey wrote <path>" while 0 rows landed).
        total_rows = (sum(1 for ln in open(inv_path) if ln.strip())
                      if inv_path.exists() else 0)
        print(f"\nsurvey: {n_new} events this run -- {n_rows} rows written, "
              f"{n_no_data} no-data, {n_empty_accepted} accepted-empty, "
              f"{n_empty_retry} resolved-but-empty (retry next run), "
              f"{n_incomplete} incomplete", flush=True)
        if total_rows == 0:
            print(
                "[warn] inventory.jsonl is EMPTY (0 rows). Every surveyed event "
                "resolved to zero retrievable files, so nothing was written -- "
                "usually the environment, not the survey. Sanity-check one event: "
                "`datatrail ps <scope> <event> -s` (is a 'Common Path:' line "
                "printed?), then `cadcinfo --cert ~/.ssl/cadcproxy.pem <cadc-uri>` "
                "for one freq_id (NotFound = the bytes aged off storage, or a size "
                "under the 1 MiB floor; pass the cert or the CLI runs anonymously and "
                "reports a misleading 'Unauthorized'). The lowest event IDs are the "
                "likeliest to have aged out of the archive, so a larger "
                "--max-events often starts filling the inventory.", flush=True)
        print(f"survey wrote {inv_path}", flush=True)
        return str(inv_path)

    # -- doctor --------------------------------------------------------------
    def preflight(self, ctx: RunContext) -> tuple[bool, list[str], list[str]]:
        problems: list = []
        notes: list = []           # non-fatal "couldn't check" caveats (doctor: [--])
        cert = _default_cert()
        if not os.path.exists(cert):
            problems.append(
                f"no CADC proxy cert at {cert} "
                f"(run `cadc-get-cert -u <user>` or set CADC_CERT)")
        try:
            import cadcdata  # noqa: F401
            import cadcutils  # noqa: F401
        except Exception:
            problems.append("cadcdata/cadcutils not importable "
                            "(pip install -e \".[cadc]\")")

        # -- survey-only prerequisites: datatrail ------------------------------
        # scan/fetch never call datatrail; these are survey-only and stay silent
        # when datatrail is absent (`requires` already lists it). The whole
        # datatrail surface lives behind the Datatrail adapter (see _datatrail.py).
        if Datatrail.installed():
            # (a) the datatrail Python API survey calls (listing + common-path).
            # dtcli.src.functions is an internal module; if an upgrade moves or
            # renames a function, the live call would misread as a service outage
            # and stall survey ~an hour before a misleading 'expired cert' abort.
            # Report the real cause here, before the run, instead.
            ok, detail = Datatrail.api_available()
            if not ok:
                problems.append(
                    "datatrail is installed but the Python API datatrawl calls is "
                    f"unavailable: {detail}. survey uses dtcli.src.functions "
                    "(`list` + `find_dataset_common_path`); pin datatrail-cli or "
                    "update the adapter (scan/fetch are unaffected).")

            # (b) validate the scope(s) survey will walk against datatrail's live
            # namespace, so a stale/renamed scope fails here instead of silently
            # walking nothing into an empty inventory. datatrail owns which scopes
            # EXIST; the instrument YAML owns which to walk.
            o = ctx.options or {}
            scope_opt = o.get("scope")
            effective = (tuple(s.strip() for s in
                              (scope_opt.split(",") if isinstance(scope_opt, str)
                               else scope_opt) if str(s).strip())
                         if scope_opt
                         else tuple(getattr(ctx.instrument, "scopes", ()) or ()))
            if effective:
                try:
                    known = set(DATATRAIL.list_scopes())
                except Exception:
                    known = set()    # datatrail unreachable -> can't validate
                if known:            # empty => transient/auth, not 'all invalid'
                    missing = [s for s in effective if s not in known]
                    if missing:
                        problems.append(
                            f"scope(s) not found in datatrail: {missing} -- survey "
                            "would walk nothing for them. Fix the instrument YAML "
                            "`scopes:` or --scope (`datatrail ls` lists valid "
                            "scopes).")
                else:
                    # couldn't list scopes (datatrail down / not on PATH). Don't
                    # claim the scopes are invalid -- surface a visible, non-fatal
                    # 'skipped' so doctor's READY isn't silently masking a check
                    # that never actually ran.
                    notes.append(
                        "datatrail scope(s) not validated: could not reach "
                        "datatrail to list scopes -- survey will still attempt "
                        f"{list(effective)} (re-run doctor once datatrail responds)")

        return (not problems), problems, notes