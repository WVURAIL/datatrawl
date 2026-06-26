"""
datatrawl CLI.

Discovery (answer "how do I even start?"):
  datatrawl list                 everything available, at a glance
  datatrawl list telescopes      telescopes (ready vs. stub)
  datatrawl list readers         file-format readers
  datatrawl list analyzers        analyses
  datatrawl list sources         where data comes from
  datatrawl doctor               the startup checklist + ready-to-go combos
  datatrawl doctor --telescope chime --source cadc-datatrail \
                       --reader chime-baseband --analyzer spectrum
                                     checklist for ONE chosen combination

Run:
  datatrawl explore --telescope chime --source cadc-datatrail
                                     what data is available (no download)
  datatrawl survey --telescope chime --source cadc-datatrail   # -> data/chime/
  datatrawl scan   --name chime --analyzer spectrum --select 614,706
                                                 # telescope/source/reader read
                                                 # from the inventory it built

The four choices -- telescope, source, reader, analyzer -- are the whole model.
`survey` writes a named inventory dir (data/<telescope>-fid<freq_ids>/, or --name) and
records the first three choices in it, so `scan` only needs the analyzer plus which
inventory (--name or --inventory); pass --telescope/--source/--reader to override.
`doctor` exists to make those choices and their prerequisites legible before you
spend hours on a scan.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from typing import List, Sequence

from . import __version__
from . import instruments as inst_mod
from . import registry
from .interfaces import (RunContext, READY, EXPERIMENTAL, STUB,
                         SurveyUnavailableError)


# --------------------------------------------------------------------------
# tiny ASCII table helper (no deps; renders cleanly over SSH on CANFAR)
# --------------------------------------------------------------------------
def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    cols = list(zip(*([headers] + list(rows)))) if rows else [(h,) for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = "  ".join("{:<{}}".format(h, w) for h, w in zip(headers, widths))
    sep = "  ".join("-" * w for w in widths)
    out = [line, sep]
    for r in rows:
        out.append("  ".join("{:<{}}".format(str(c), w)
                             for c, w in zip(r, widths)))
    return "\n".join(out)


_MARK = {True: "[OK]", False: "[ ]"}
_SKIP = "[--]"          # a check that could not run (non-fatal); see doctor notes


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
def _list_telescopes() -> str:
    rows = []
    for r in inst_mod.all_readiness():
        miss = ", ".join(r.missing()) or "-"
        rows.append([r.name, r.status, miss])
    return ("Telescopes (instruments/*.yaml)\n" +
            _table(["name", "status", "missing"], rows))


def _list_kind(kind: str, title: str) -> str:
    rows = []
    for info in registry.describe(kind):
        rows.append([info.name, info.status,
                     ", ".join(info.instruments) or "-", info.summary])
    return f"{title}\n" + _table(["name", "status", "instruments", "summary"], rows)


def cmd_list(args) -> int:
    what = (args.what or "all").lower()
    blocks = []
    if what in ("all", "telescopes", "instruments"):
        blocks.append(_list_telescopes())
    if what in ("all", "sources", "source"):
        blocks.append(_list_kind("source", "Data sources (where data lives)"))
    if what in ("all", "readers", "reader"):
        blocks.append(_list_kind("reader", "Readers (file formats)"))
    if what in ("all", "analyzers", "analyzer", "analyses"):
        blocks.append(_list_kind("analyzer", "Analyzers (science plugins)"))
    if not blocks:
        print(f"unknown list target {args.what!r}; "
              f"try: telescopes | sources | readers | analyzers | all",
              file=sys.stderr)
        return 2
    print("\n\n".join(blocks))
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def _ready_combos() -> List[tuple]:
    """(telescope, source, reader, analyzer) tuples where every part is usable.

    A part is "usable" when its plugin is READY; a telescope is usable when it
    satisfies the chosen source's config needs (full archive config for CADC,
    geometry + Nyquist zone only for a local source).
    """
    readiness = {r.name: r for r in inst_mod.all_readiness()}
    srcs = [i for i in registry.describe("source") if i.status == READY]
    rdrs = [i for i in registry.describe("reader") if i.status == READY]
    reds = [i for i in registry.describe("analyzer") if i.status == READY]
    combos = []
    for red in reds:
        for rdr in rdrs:
            # only pair readers/analyzers that share at least one instrument
            shared = set(rdr.instruments) & set(red.instruments)
            if not (shared or "*" in rdr.instruments or "*" in red.instruments):
                continue
            # instruments the reader+analyzer jointly support ("*" = any known)
            names = set(readiness)
            if "*" not in rdr.instruments and rdr.instruments:
                names &= set(rdr.instruments)
            if "*" not in red.instruments and red.instruments:
                names &= set(red.instruments)
            for src in srcs:
                src_names = (names if "*" in src.instruments or not src.instruments
                             else names & set(src.instruments))
                for tel in sorted(src_names):
                    r = readiness.get(tel)
                    if r is not None and r.usable_for(src.needs_archive_config):
                        combos.append((tel, src.name, rdr.name, red.name))
    return combos


def _overview_checklist() -> str:
    lines = [
        "datatrawl -- readiness check",
        "=" * 28,
        "",
        "datatrawl has two jobs:",
        "",
        "  survey   build a local inventory from Datatrail/CADC or another source",
        "  scan     stream inventory units through a reader and analyzer",
        "",
        "For a scan, the core choices are:",
        "",
        "  1. source      where files are staged from        (list sources)",
        "  2. reader      the file format                    (list readers)",
        "  3. analyzer    science plugin to run             (list analyzers)",
        "",
        "For telescope archive data, also choose:",
        "",
        "  4. telescope   band/channelization geometry      (list telescopes)",
        "",
        "Examples:",
        "",
        "  # Datatrail survey, then scan by inventory name",
        "  datatrawl survey --telescope chime --source cadc-datatrail \\",
        "      --scope chime.event.baseband.raw --freq-ids 844 --name <name>",
        "  datatrawl explore --name <name>",
        "  datatrawl scan --name <name> --analyzer spectrum --select 844",
        "",
        "  # local files already on disk",
        "  datatrawl scan --source local --source-root <dir> --telescope chime \\",
        "      --reader chime-baseband --analyzer spectrum --select 844",
        "",
    ]

    combos = _ready_combos()
    if combos:
        lines.append("Ready scan combinations:")
        rows = [[t, s, r, a] for (t, s, r, a) in combos]
        lines.append("  " + _table(["telescope", "source", "reader", "analyzer"],
                                    rows).replace("\n", "\n  "))
        lines.append("")

    stubs = [f"{i.name} ({k})" for k in ("reader", "analyzer")
             for i in registry.describe(k) if i.status == STUB]
    if stubs:
        lines.append("Stubs awaiting implementation: " + ", ".join(sorted(stubs)))
        lines.append("  (visible in `list`, but `scan` will refuse until built)")
        lines.append("")

    lines.append("Run `doctor` with a chosen combination for exact prerequisites, e.g.:")
    lines.append("  datatrawl doctor --telescope chime --source local --reader chime-baseband --analyzer spectrum")
    return "\n".join(lines)


def _check(ok: bool, label: str, fix: str = "") -> str:
    mark = _MARK[bool(ok)]
    if ok or not fix:
        return f"  {mark} {label}"
    return f"  {mark} {label}\n         -> {fix}"


def cmd_doctor(args) -> int:
    chose_any = any([args.telescope, args.source, args.reader, args.analyzer])
    if not chose_any:
        print(_overview_checklist())
        return 0

    print("datatrawl -- readiness check")
    print("=" * 28)
    all_ok = True
    any_skipped = False

    # Does the chosen source require the telescope's archive-access fields?
    # Consult the plugin's declared flag rather than hardcoding source names.
    src_needs_archive = False
    if args.source:
        try:
            src_needs_archive = registry.get("source", args.source).info.needs_archive_config
        except KeyError:
            src_needs_archive = False

    # --- telescope ---
    if args.telescope:
        try:
            rd = inst_mod.instrument_readiness(args.telescope)
            print("Telescope:", args.telescope)
            print(_check(rd.nyquist_zone_set, "Nyquist zone set",
                         f"set nyquist_zone in instruments/{args.telescope}.yaml"))
            # a built-in survey scope only matters for an archive source
            if src_needs_archive:
                print(_check(rd.scopes_set, "datatrail scope(s) set",
                             f"add a scopes: [...] list in "
                             f"instruments/{args.telescope}.yaml (or pass --scope)"))
            all_ok &= rd.nyquist_zone_set
            if src_needs_archive:
                all_ok &= rd.scopes_set
        except Exception as exc:                       # noqa: BLE001
            print(_check(False, f"telescope '{args.telescope}'", str(exc)))
            all_ok = False
    else:
        print(_check(False, "telescope chosen", "pass --telescope <name>"))
        all_ok = False

    # build a context for plugin preflights (best-effort; needs a valid instrument)
    ctx = None
    if args.telescope:
        try:
            instrument = inst_mod.load_instrument(args.telescope)
            ctx = RunContext(instrument=instrument,
                             options=_collect_options(args))
        except Exception:
            ctx = None

    # --- source ---
    if args.source:
        _ok, _sk = _doctor_plugin("source", args.source, ctx, archive_note=True)
        all_ok &= _ok
        any_skipped |= _sk
    else:
        print(_check(False, "source chosen", "pass --source <name>"))
        all_ok = False

    # --- reader ---
    if args.reader:
        _ok, _sk = _doctor_plugin("reader", args.reader, ctx)
        all_ok &= _ok
        any_skipped |= _sk
    else:
        print(_check(False, "reader chosen", "pass --reader <name>"))
        all_ok = False

    # --- analyzer ---
    if args.analyzer:
        _ok, _sk = _doctor_plugin("analyzer", args.analyzer, ctx)
        all_ok &= _ok
        any_skipped |= _sk
    else:
        print(_check(False, "analyzer chosen", "pass --analyzer <name>"))
        all_ok = False

    # --- cross-cutting deps ---
    print("Environment:")
    env_ok = _importable("numpy")
    print(_check(env_ok, "numpy", "pip install numpy"))
    all_ok &= env_ok
    if args.reader == "chime-baseband":
        env_ok = _importable("h5py")
        print(_check(env_ok, "h5py (CHIME baseband reader)", "pip install h5py"))
        all_ok &= env_ok
    if args.gpu:
        env_ok = _importable("cupy")
        print(_check(env_ok, "cupy (for --gpu)",
                     "run `datatrawl setup-cupy --install` to add the matching cupy"))
        all_ok &= env_ok

    print("")
    if all_ok and any_skipped:
        print("READY: core checks passed, but some were SKIPPED (see [--] above) -- "
              "re-run doctor once datatrail is reachable to validate them.")
    elif all_ok:
        print("READY: all checks passed -- you can scan.")
    else:
        print("NOT READY: resolve the unchecked items above, then re-run doctor.")
    return 0 if all_ok else 1


def _doctor_plugin(kind: str, name: str, ctx,
                   archive_note: bool = False) -> tuple[bool, bool]:
    label = kind.capitalize()
    try:
        cls = registry.get(kind, name)
    except KeyError as exc:
        print(f"{label}: {name}")
        print(_check(False, f"{kind} '{name}' registered", str(exc)))
        return False, False
    info = cls.info
    print(f"{label}: {name}  ({info.status})")
    if info.status == STUB:
        print(_check(False, f"{kind} implemented",
                     f"'{name}' is a stub -- see its module for the TODO checklist"))
        return False, False
    if info.status == EXPERIMENTAL:
        print(_check(True, f"{kind} implemented (experimental)"))
    else:
        print(_check(True, f"{kind} implemented"))
    # run the plugin's own preflight if we could build a context
    if ctx is not None:
        try:
            inst = cls()
            result = inst.preflight(ctx)
            ok, problems = result[0], result[1]
            notes = result[2] if len(result) > 2 else []
            for n in notes:                            # non-fatal "couldn't check"
                print(f"  {_SKIP} {n}")
            if ok:
                print(_check(True, "prerequisites satisfied"))
            for p in problems:
                print(_check(False, "prerequisite", p))
            return ok, bool(notes)
        except Exception as exc:                       # noqa: BLE001
            print(_check(False, "preflight", f"{type(exc).__name__}: {exc}"))
            return False, False
    return True, False


def _importable(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


# --------------------------------------------------------------------------
# run: survey / scan
# --------------------------------------------------------------------------
def _parse_opt_value(v: str):
    """Best-effort typing for a --set value: bool, None, int, float, else str."""
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def _parse_set_options(pairs) -> dict:
    """Turn repeated `--set key=value` into a dict the analyzer reads via ctx.options."""
    out = {}
    for item in (pairs or ()):
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = _parse_opt_value(v)
    return out


def _collect_options(args) -> dict:
    opts = {
        "root": getattr(args, "root", "."),
        "inventory": getattr(args, "inventory", None),
        "source_root": getattr(args, "source_root", None),
        "source_glob": getattr(args, "source_glob", None) or "*.h5",
        "source_freq_id_regex": getattr(args, "source_freq_id_regex", None),
        "gpu": getattr(args, "gpu", False),
        "max_events": getattr(args, "max_events", None),
        # survey-only knobs (absent on other subcommands -> harmless None/False)
        "scope": getattr(args, "scope", None),
        "freq_ids": getattr(args, "freq_ids", None),
        "include_outrigger": getattr(args, "include_outrigger", False),
        "workers": getattr(args, "workers", None),
        "re_enumerate": getattr(args, "re_enumerate", False),
        "scopes_only": getattr(args, "scopes_only", False),
        "match": getattr(args, "match", None),
        "max_inspect": getattr(args, "max_inspect", None),
    }
    # Analyzer-specific parameters travel through ctx.options, set generically with
    # --set key=value (e.g. --set bracket_hz=400 for a detection analyzer), so the
    # CLI stays analysis-agnostic.
    opts.update(_parse_set_options(getattr(args, "set_opts", None)))
    return opts


def _make_ctx(args):
    instrument = inst_mod.load_instrument(args.telescope)
    opts = {k: v for k, v in _collect_options(args).items() if v is not None}
    return instrument, RunContext(instrument=instrument, options=opts)


# --------------------------------------------------------------------------
# Inventory metadata sidecar
#
# `survey` writes a small `<inventory>.meta.json` next to inventory.jsonl
# recording how the inventory was built: telescope, source, the telescope's
# canonical reader, scope, and the freq_id selection. This does two jobs:
#   * provenance -- the inventory now records how it was produced; and
#   * ergonomics -- `scan` reads it to backfill --telescope/--source/--reader,
#     so the common case is `scan --inventory <path> --analyzer <R>`. The four
#     axes are still the whole model; they're just inferred from the inventory
#     the survey already pinned down. Explicit flags always win.
# --------------------------------------------------------------------------
def _meta_path_for(inventory_path: str) -> str:
    return os.path.splitext(inventory_path)[0] + ".meta.json"


def _freq_id_slug(freq_ids) -> str:
    """A short, filesystem-safe slug for a freq_id selection ('' means 'all')."""
    if not freq_ids or str(freq_ids).strip().lower() in ("all", "*"):
        return ""
    s = str(freq_ids).strip().lower().replace(" ", "").replace(",", "-")
    s = re.sub(r"[^a-z0-9._-]", "", s)
    if s and s[0].isdigit():
        s = "fid" + s
    if len(s) > 40:                       # pathological selections -> short hash
        s = "fid" + hashlib.sha1(s.encode()).hexdigest()[:8]
    return s


def derive_inventory_name(telescope: str, freq_ids) -> str:
    """Default inventory name, deterministic from the survey spec so the same
    survey resolves to the same dir (and resumes) while different selections get
    their own. No timestamp, by design -- that determinism is what keeps resume
    working."""
    slug = _freq_id_slug(freq_ids)
    return f"{telescope}-{slug}" if slug else telescope


def _scopes_in_inventory(inventory_path) -> list:
    """Unique `scope` values across the inventory's rows, in first-seen order.

    The source stamps each row with the scope it came from, so this reflects what
    was *actually* surveyed -- including the source's resolved defaults when
    `--scope` was omitted (the CADC source walks two CHIME scopes by default)."""
    seen: list = []
    try:
        with open(inventory_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                s = r.get("scope")
                if s and s not in seen:
                    seen.append(s)
    except OSError:
        pass
    return seen


def write_inventory_meta(inventory_path, instrument, source, freq_ids=None,
                         name=None, scope_request=None, reader=None) -> str:
    """Stamp the sidecar describing how this inventory was built.

    `scopes` lists the scope(s) actually surveyed (read back from the rows, so the
    source's resolved defaults are captured, not just the YAML default), `scope`
    is their comma-joined form for readability, and `scope_request` preserves the
    raw `--scope` the user passed (if any)."""
    scopes = _scopes_in_inventory(inventory_path)
    if not scopes:                                   # empty/sparse inventory
        if scope_request:
            scopes = [s.strip() for s in scope_request.split(",") if s.strip()]
        elif getattr(instrument, "scopes", None):
            scopes = list(instrument.scopes)
    meta = {
        "datatrawl_inventory": 1,
        "name": name,
        "telescope": instrument.name,
        "source": source,
        "reader": reader or getattr(instrument, "reader", "") or None,
        "scope": ",".join(scopes) if scopes else None,
        "scopes": scopes or None,
        "scope_request": scope_request or None,
        "freq_ids": freq_ids,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }
    meta_path = _meta_path_for(inventory_path)
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    return meta_path


def _sole_inventory(root: str):
    """The single per-telescope inventory under data/, if there's exactly one."""
    hits = glob.glob(os.path.join(root, "data", "*", "inventory.jsonl"))
    return hits[0] if len(hits) == 1 else None


def _resolve_from_meta(args) -> None:
    """Backfill --telescope/--source/--reader (and --inventory) from an
    inventory's sidecar meta. Locates the inventory from --inventory, else the
    telescope's default dir, else the sole inventory under data/. Explicit flags
    are never overwritten; a missing or unreadable sidecar is a silent no-op."""
    inv = getattr(args, "inventory", None)
    root = getattr(args, "root", os.getcwd())
    if inv is None and getattr(args, "name", None):
        inv = os.path.join(root, "data", args.name, "inventory.jsonl")
    if inv is None and getattr(args, "telescope", None):
        inv = os.path.join(root, "data", args.telescope, "inventory.jsonl")
    if inv is None:
        inv = _sole_inventory(root)
    if not inv:
        return
    meta_path = _meta_path_for(inv)
    if not os.path.exists(meta_path):
        return
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return
    for key in ("telescope", "source", "reader"):
        if getattr(args, key, None) is None and meta.get(key):
            setattr(args, key, meta[key])
    if getattr(args, "inventory", None) is None:
        args.inventory = inv


def cmd_survey(args) -> int:
    instrument, ctx = _make_ctx(args)
    src = _require_plugin("source", args.source)()
    if getattr(args, "scopes_only", False) and not args.out:
        # the recon scope map spans telescopes (it lists every datatrail scope),
        # so it is not telescope-specific -> data/scopes.jsonl, a level above the
        # per-telescope inventory dirs.
        out_dir = os.path.join(args.root, "data")
        name = None
    elif args.out:
        out_dir = args.out
        name = getattr(args, "name", None) or os.path.basename(
            os.path.normpath(args.out))
    else:
        # name the inventory deterministically from the survey spec, so multiple
        # projects on one telescope land in separate dirs (and an identical
        # re-survey resumes the same one). --name overrides the derived label.
        name = getattr(args, "name", None) or derive_inventory_name(
            instrument.name, getattr(args, "freq_ids", None))
        out_dir = os.path.join(args.root, "data", name)
    print(f"[survey] {instrument.name} via {args.source} -> {out_dir}")
    if args.dry_run:
        print("  dry-run: would survey")
        return 0
    try:
        path = src.survey(ctx, out_dir)
    except NotImplementedError as exc:
        print(f"error: {exc}\n"
              "This source enumerates on demand; use `datatrawl explore` / "
              "`datatrawl scan` directly, or implement survey() to write a "
              "persistent inventory.", file=sys.stderr)
        return 2
    except SurveyUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    label = "scope map" if getattr(args, "scopes_only", False) else "inventory"
    print(f"  {label}: {path}")
    if not getattr(args, "scopes_only", False):
        meta_path = write_inventory_meta(
            path, instrument, args.source, getattr(args, "freq_ids", None),
            name=name, scope_request=getattr(args, "scope", None))
        print(f"  meta: {meta_path}")
    return 0


# --------------------------------------------------------------------------
# explore: "what is available?" -- no download, no reduction
# --------------------------------------------------------------------------
_FREQ_ID_RE = re.compile(r"_(\d+)\.h5$")


def _human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if x < 1024 or unit == "PB":
            return f"{int(x)} B" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} PB"


def _freq_id_of(unit):
    """The freq_id for a unit: from its metadata, else parsed from the name."""
    m = unit.meta or {}
    if m.get("freq_id") is not None:
        try:
            return int(m["freq_id"])
        except (TypeError, ValueError):
            return None
    mo = _FREQ_ID_RE.search(unit.name or "")
    return int(mo.group(1)) if mo else None


def _print_availability(units, source_name: str, instrument, scan_hint=None) -> None:
    from collections import Counter
    by_freq_id: "Counter" = Counter()
    dates: List[str] = []
    nbytes = 0
    n_no_freq_id = 0
    for u in units:
        fid = _freq_id_of(u)
        if fid is None:
            n_no_freq_id += 1
        else:
            by_freq_id[fid] += 1
        m = u.meta or {}
        d = str(m.get("obs_date") or "")[:10]
        if d:
            dates.append(d)
        sb = m.get("size_bytes")
        if not sb:
            p = m.get("src_path")
            if p and os.path.exists(p):
                try:
                    sb = os.path.getsize(p)
                except OSError:
                    sb = 0
        nbytes += int(sb or 0)

    tel = f" for telescope '{instrument.name}'" if instrument else ""
    print(f"Available via source '{source_name}'{tel}")
    print(f"  files          : {len(units)}")
    if nbytes:
        print(f"  total volume   : {_human_bytes(nbytes)}")
    if dates:
        print(f"  date span      : {min(dates)} .. {max(dates)}")
    if by_freq_id:
        freq_ids = sorted(by_freq_id)
        print(f"  freq_ids       : {len(freq_ids)} present ({freq_ids[0]}..{freq_ids[-1]})")
        rows = [[str(f), str(by_freq_id[f])] for f in freq_ids]
        print("    " + _table(["freq_id", "files"], rows).replace("\n", "\n    "))
    if n_no_freq_id:
        print(f"  ({n_no_freq_id} file(s) with no parseable freq_id)")
    if by_freq_id:
        sample = ",".join(str(f) for f in sorted(by_freq_id)[:3])
        print("\nTo run an analyzer on one or more of these freq_ids, add --select, e.g.:")
        if scan_hint:
            print(f"  datatrawl scan {scan_hint} --analyzer <analyzer> --select {sample}")
            print("    (--name points back at this inventory; the telescope, source, and")
            print("     reader are read from it, so you don't repeat those flags)")
        else:
            print(f"  datatrawl scan --analyzer <analyzer> --select {sample}")
            print("    (a surveyed inventory stores telescope/source/reader; a local")
            print("     directory has none, so pass --telescope, --source, and --reader)")


def cmd_explore(args) -> int:
    """Report WHAT is available for a source -- no download, no reduction.

    For the exploratory 'I don't know my selection yet' case: enumerate the
    holdings (an inventory for an archive source, a directory for local) and
    summarize them -- freq_ids present, file counts, date span, total volume --
    so a selection can be chosen empirically before committing to a scan.
    """
    # Resolve --name/--inventory (and backfill telescope/source/reader) from an
    # inventory's meta sidecar, exactly as `scan` does -- so `explore --name X`
    # finds the same inventory `survey` wrote instead of the telescope default.
    _resolve_from_meta(args)
    if not getattr(args, "source", None):
        print("error: --source not provided and not resolvable from an inventory "
              "meta. Pass --source (e.g. `--source local` or "
              "`--source cadc-datatrail`), or --name/--inventory pointing at a "
              "surveyed inventory (survey records the source).", file=sys.stderr)
        return 2
    src = _require_plugin("source", args.source)()
    instrument = None
    if args.telescope:
        try:
            instrument = inst_mod.load_instrument(args.telescope)
        except Exception as exc:                              # noqa: BLE001
            print(f"note: could not load telescope '{args.telescope}': {exc}",
                  file=sys.stderr)
    opts = {
        "root": getattr(args, "root", os.getcwd()),
        "inventory": getattr(args, "inventory", None),
        "source_root": getattr(args, "source_root", None),
        "source_glob": getattr(args, "source_glob", None) or "*.h5",
        "source_freq_id_regex": getattr(args, "source_freq_id_regex", None),
    }
    opts.update(_parse_set_options(getattr(args, "set_opts", None)))
    ctx = RunContext(instrument=instrument, selection=None, options=opts)
    try:
        units = list(src.enumerate(ctx))
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:                                  # noqa: BLE001
        print(f"could not enumerate: {type(exc).__name__}: {exc}\n"
              f"(an archive source needs --telescope, to locate its inventory, "
              f"or an explicit --inventory)", file=sys.stderr)
        return 1
    if not units:
        print("no data found. For an archive source, run `survey` first or pass "
              "--inventory; for local, check --source-root / --source-glob.")
        return 1
    # Suggest a scan that points at the same inventory: prefer --name when the
    # caller used it, else the resolved --inventory path; None for a local source.
    if getattr(args, "name", None):
        scan_hint = f"--name {args.name}"
    elif getattr(args, "inventory", None):
        scan_hint = f"--inventory {args.inventory}"
    else:
        scan_hint = None
    _print_availability(units, args.source, instrument, scan_hint=scan_hint)
    return 0


def _require_plugin(kind: str, name: str):
    """registry.get(), but turn a missing plugin into a clean, actionable error.

    A surveyed inventory records source/reader names, but a custom module named
    there is not auto-loaded. It must still be passed with --plugin (or discovered
    through the environment / an entry point). Without this helper, registry.get()
    raises a bare KeyError as an uncaught traceback.
    """
    try:
        return registry.get(kind, name)
    except KeyError as exc:
        msg = exc.args[0] if exc.args else str(exc)
        raise SystemExit(
            f"{msg}\n  If {name!r} is a custom {kind} from your own project, load "
            f"it with --plugin (or DATATRAWL_PLUGINS / an entry point) -- a "
            f"source/reader/analyzer named in an inventory's meta is not "
            f"auto-loaded. `datatrawl list` shows what is currently registered.")


def _path_component(value) -> str:
    """A short filesystem-safe component for plugin names and temp prefixes."""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return text or "unknown"


def _scan_tmp_dir(args, instrument) -> tuple[str, bool]:
    """Return (directory, is_auto_created) for this scan invocation.

    Explicit --tmp-dir wins. Otherwise DATATRAWL_TMPDIR is the base, followed by
    a writable /scratch, then the platform temp directory. Auto-created paths are
    unique per invocation so concurrent scans cannot delete each other's files.
    """
    if getattr(args, "tmp_dir", None):
        return os.path.abspath(os.path.expanduser(args.tmp_dir)), False

    base = os.environ.get("DATATRAWL_TMPDIR")
    if base:
        base = os.path.abspath(os.path.expanduser(base))
    elif os.path.isdir("/scratch") and os.access("/scratch", os.W_OK):
        base = "/scratch"
    else:
        base = tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)

    prefix = (f"datatrawl_{_path_component(instrument.name)}_"
              f"{_path_component(args.analyzer)}_")
    return tempfile.mkdtemp(prefix=prefix, dir=base), True


def _default_quarantine_path(args, instrument) -> str:
    """Source/reader-scoped quarantine ledger for this telescope."""
    name = (f"{_path_component(args.source)}--"
            f"{_path_component(args.reader)}.jsonl")
    return os.path.join(args.root, "results", instrument.name, "quarantine", name)


def cmd_scan(args) -> int:
    from . import pipeline
    _resolve_from_meta(args)            # backfill telescope/source/reader from the
                                        # inventory's sidecar; explicit flags win
    missing = [f"--{k}" for k in ("telescope", "source", "reader")
               if not getattr(args, k, None)]
    if missing:
        print(f"error: {', '.join(missing)} not provided and not resolvable from "
              f"an inventory meta. Run `survey` first (it records telescope, "
              f"source and reader), pass --inventory pointing at a surveyed "
              f"inventory, or set them explicitly.", file=sys.stderr)
        return 2
    instrument, ctx = _make_ctx(args)
    # --nfft overrides the analysis frame/FFT length for this run; the reader and
    # analyzer both read it from ctx.instrument.nfft. fs is unaffected (it comes
    # from the band geometry, not nfft).
    if getattr(args, "nfft", None):
        instrument.nfft = int(args.nfft)
    src = _require_plugin("source", args.source)()
    rdr = _require_plugin("reader", args.reader)()
    red_cls = _require_plugin("analyzer", args.analyzer)

    # The analysis splits --select into independent runs (one product each).
    # For the spectrum analyzer that is one run per freq_id: each <freq_id>.npz
    # checkpoints and resumes on its own, which is what makes a long multi-freq_id
    # pull self-healing rather than all-or-nothing.
    runs = red_cls().plan_runs(ctx, args.select)
    if not runs:
        print("nothing to do: --select resolved to an empty set", file=sys.stderr)
        return 1
    if args.out and len(runs) > 1:
        print("error: --out names a single file but this selection fans out to "
              f"{len(runs)} products. Omit --out (they go to "
              f"results/{instrument.name}/{args.analyzer}/<stem>.npz) or scan "
              "one at a time.",
              file=sys.stderr)
        return 1

    tmp, auto_tmp = _scan_tmp_dir(args, instrument)

    # Quarantine is scoped to the source/reader pair. The ledger stores stable
    # unit identities, so unrelated files that share a basename are not excluded.
    if getattr(args, "no_quarantine", False):
        quarantine_path = None
    else:
        quarantine_path = args.quarantine or _default_quarantine_path(args, instrument)

    print(f"[scan] {instrument.name}  source={args.source}  reader={args.reader}  "
          f"analyzer={args.analyzer}  ({len(runs)} product(s))")

    total_failed = 0
    total_quarantined = 0
    done_products = 0
    try:
        for i, sub_sel in enumerate(runs, 1):
            ctx.selection = sub_sel
            units = list(src.enumerate(ctx))
            out = args.out or _default_product_path(args, instrument, sub_sel)
            tag = f"[{i}/{len(runs)}] select={sub_sel}"
            if not units:
                print(f"  {tag}: no units matched -- skipping", flush=True)
                continue
            print(f"  {tag}  units={len(units)} -> {out}", flush=True)
            if args.dry_run:
                for u in units[:3]:
                    print(f"      would process: {u.name}")
                if len(units) > 3:
                    print(f"      ... and {len(units) - 3} more")
                continue
            red = red_cls()                     # FRESH analyzer per product
            res = pipeline.run(
                source=src, reader=rdr, analyzer=red, units=units,
                out_path=out, tmp_dir=tmp, ctx=ctx,
                checkpoint_every=args.checkpoint_every,
                download_workers=args.download_workers,
                max_staged_files=args.max_staged_files,
                max_files=args.max_files,
                max_frames_per_file=args.max_frames_per_file,
                quarantine_path=quarantine_path,
                verbose=(len(runs) == 1),       # quiet per-file noise for big fan-outs
            )
            total_failed += res.n_failed
            total_quarantined += res.n_quarantined
            done_products += 1

        if not args.dry_run:
            msg = (f"\nscan complete: {done_products}/{len(runs)} product(s), "
                   f"{total_failed} file failure(s)")
            if total_quarantined:
                msg += (f", {total_quarantined} file(s) quarantined as bad "
                        f"(see {quarantine_path})")
            if total_failed:
                msg += (" -- re-run the same command to retry the failures (resume "
                        "skips everything already done).")
            print(msg)
        return 0 if total_failed == 0 else 1
    finally:
        if auto_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _default_product_path(args, instrument, selection) -> str:
    # Namespaced by analyzer so two analyses never collide on the same
    # <freq_id>.npz (e.g. spectrum's products live under results/<tel>/spectrum/).
    base = os.path.join(args.root, "results", instrument.name, args.analyzer)
    if isinstance(selection, (list, tuple)):
        stem = "_".join(str(s) for s in selection) if selection else "all"
    elif selection is None:
        stem = "all"
    else:
        stem = str(selection).strip().replace(",", "_").replace(" ", "") or "all"
    return os.path.join(base, f"{stem}.npz")


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def positive_int(s: str) -> int:
    """argparse type: a strictly positive integer (rejects 0 and negatives)."""
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {s!r}")
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {v}")
    return v


def cmd_setup_cupy(args) -> int:
    """Install the CuPy build matching this CANFAR session's CUDA, for GPU analyzers.

    datatrawl prefers the CuPy the session image already ships, so this only acts when
    the image has none. It calls datatrawl.accel.ensure_cupy -- the same resolution a
    scan uses to pick an array module -- so a scan itself never installs anything; it
    only uses what is importable.
    """
    from . import accel

    cp = accel.import_cupy()
    if cp is not None:
        print(f"[gpu] cupy {getattr(cp, '__version__', '?')} already available from the "
              "session image -- nothing to do.")
        return 0

    major = accel.detect_cuda_major()
    if major is None:
        print("[gpu] cupy is not installed and the CUDA version could not be detected "
              "(no nvidia-smi/nvcc and no CUDA version file).", file=sys.stderr)
        print("      Install the cupy build matching your image manually, "
              "e.g. `pip install cupy-cuda12x`.", file=sys.stderr)
        return 1

    pkg = accel.cupy_package(major)
    if not args.install:
        print(f"[gpu] no cupy in this environment. Detected CUDA {major}.x.")
        print(f"      Re-run with --install to install {pkg}.")
        return 1

    try:
        cp = accel.ensure_cupy(install=True)
    except Exception as exc:
        print(f"[gpu] {exc}", file=sys.stderr)
        return 1
    print(f"[gpu] installed and imported cupy {getattr(cp, '__version__', '?')} "
          f"({pkg}) -- scan --gpu is ready.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="datatrawl", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Shared by every subcommand: load extra plugin modules so an analyzer/reader/
    # source living in YOUR project (not this repo) becomes first-class here.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--plugin", action="append", default=[], metavar="MODULE_OR_PATH",
        help="import an external plugin so it registers (repeatable). A dotted "
             "module ('mypkg.analyzers.fstat') or a path to a .py file. Also "
             "honoured via the DATATRAWL_PLUGINS env var and entry points.")

    p_list = sub.add_parser("list", parents=[common],
                            help="show available telescopes/sources/readers/analyzers")
    p_list.add_argument("what", nargs="?", default="all",
                        help="telescopes | sources | readers | analyzers | all")
    p_list.set_defaults(func=cmd_list)

    p_doctor = sub.add_parser("doctor", parents=[common],
                              help="startup checklist for a chosen combination")
    p_doctor.add_argument("--telescope")
    p_doctor.add_argument("--source")
    p_doctor.add_argument("--reader")
    p_doctor.add_argument("--analyzer")
    p_doctor.add_argument("--gpu", action="store_true")
    p_doctor.add_argument("--source-root", default=None)
    p_doctor.add_argument(
        "--set", dest="set_opts", action="append", metavar="KEY=VALUE",
        help="plugin-specific parameter passed via ctx.options (repeatable)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_survey = sub.add_parser("survey", parents=[common],
                              help="walk the archive to build an inventory for a source")
    p_survey.add_argument("--telescope", required=True)
    p_survey.add_argument("--source", default="cadc-datatrail",
                          help="source plugin to use (default: cadc-datatrail)")
    p_survey.add_argument("--root", default=os.getcwd())
    p_survey.add_argument("--out", default=None)
    p_survey.add_argument("--name", default=None,
                          help="name this inventory -> data/<name>/ "
                               "(default: derived from telescope + freq_ids)")
    p_survey.add_argument("--scope", default=None,
                          help="Datatrail scope(s) to walk, comma-separated. Defaults "
                               "to the telescope's declared scopes (chime: the two "
                               "CHIME baseband scopes).")
    p_survey.add_argument("--freq-ids", default=None,
                          help="CHIME-baseband freq_id selection: a list "
                               "'614,706', a range '506-844', or 'all'.")
    p_survey.add_argument("--include-outrigger", action="store_true",
                          help="CHIME-baseband event survey only: keep events carrying an outrigger label "
                               "(default: blocklisted)")
    p_survey.add_argument("--workers", type=positive_int, default=12,
                          help="CHIME-baseband event/freq_id survey only: parallel cadcinfo probes per event (default 12)")
    p_survey.add_argument("--re-enumerate", action="store_true",
                          help="rebuild the phase-1 event cache instead of reusing it")
    p_survey.add_argument("--max-events", type=positive_int, default=None,
                          help="CHIME-baseband event/freq_id survey only: survey at "
                               "most N not-yet-done events this run, then stop "
                               "(resumable; handy for a quick smoke test).")
    p_survey.add_argument("--scopes-only", action="store_true",
                          help="recon: list datasets across the scope(s) WITHOUT "
                               "enumerating events/files (a recursive `datatrail "
                               "ls`). With no --scope, walks every scope datatrail "
                               "can see. Writes scopes.jsonl.")
    p_survey.add_argument("--match", default=None,
                          help="recon filter: comma-separated substrings; keep only "
                               "scope/dataset names containing ALL of them "
                               "(case-insensitive).")
    p_survey.add_argument("--set", dest="set_opts", action="append", metavar="KEY=VALUE",
                          help="source-specific parameter passed via ctx.options "
                               "(repeatable), for a custom source's survey(), e.g. "
                               "--set source_root=/data")
    p_survey.add_argument("--dry-run", action="store_true")
    p_survey.set_defaults(func=cmd_survey)

    p_expl = sub.add_parser(
        "explore", parents=[common],
        help="report what data is available for a source (no download)")
    p_expl.add_argument("--source", default=None,
                        help="data source; optional when --name/--inventory is "
                             "given (read from the inventory meta, like `scan`)")
    p_expl.add_argument("--telescope", default=None,
                        help="needed for an archive source (locates its inventory)")
    p_expl.add_argument("--inventory", default=None)
    p_expl.add_argument("--name", default=None,
                        help="inventory dir label under data/ set by survey; "
                             "alternative to --inventory (telescope/source/reader "
                             "are read from its meta sidecar)")
    p_expl.add_argument("--source-root", default=None,
                        help="local source: directory to inspect")
    p_expl.add_argument("--source-glob", default="*.h5")
    p_expl.add_argument("--source-freq-id-regex", default=None,
                        help="local source: regex with one group capturing the "
                             "freq_id int from a filename")
    p_expl.add_argument(
        "--set", dest="set_opts", action="append", metavar="KEY=VALUE",
        help="source-specific parameter passed via ctx.options (repeatable)")
    p_expl.add_argument("--root", default=os.getcwd())
    p_expl.set_defaults(func=cmd_explore)

    p_scan = sub.add_parser("scan", parents=[common],
                            help="run the streaming analyzer")
    p_scan.add_argument("--telescope", default=None,
                        help="telescope geometry; inferred from the inventory "
                             "meta when omitted")
    p_scan.add_argument("--source", default=None,
                        help="where files are fetched from; inferred from the "
                             "inventory meta when omitted")
    p_scan.add_argument("--reader", default=None,
                        help="file-format reader; defaults to the telescope's "
                             "canonical reader recorded in the inventory meta")
    p_scan.add_argument("--analyzer", required=True)
    p_scan.add_argument("--select", default=None,
                        help="selection passed to the analyzer, e.g. a single "
                             "freq_id '844', a list '614,706', or a range "
                             "'506-552' (spectrum needs explicit freq_ids)")
    p_scan.add_argument("--root", default=os.getcwd())
    p_scan.add_argument("--out", default=None, help="product path (default results/<tel>/<analyzer>/<sel>.npz)")
    p_scan.add_argument("--inventory", default=None)
    p_scan.add_argument("--name", default=None,
                        help="named inventory under data/<name>/ as written by "
                             "survey; alternative to --inventory")
    p_scan.add_argument("--source-root", default=None, help="local source: input dir")
    p_scan.add_argument("--source-glob", default="*.h5", help="local source: file glob")
    p_scan.add_argument("--source-freq-id-regex", default=None,
                        help="local source: regex with one group capturing the "
                             "freq_id int from a filename (default _(\\d+)\\.h5$)")
    p_scan.add_argument("--nfft", type=positive_int, default=None,
                        help="override the analysis frame/FFT length for this run "
                             "(default: the instrument YAML's nfft)")
    p_scan.add_argument(
        "--tmp-dir", default=None,
        help="scratch directory for staged files. Default: a unique directory "
             "under DATATRAWL_TMPDIR, writable /scratch, or the OS temp directory")
    p_scan.add_argument("--checkpoint-every", type=positive_int, default=50)
    p_scan.add_argument("--download-workers", type=positive_int, default=1,
                        help="parallel download threads (default 1; >1 overlaps "
                             "downloads but delivers files in completion order, so "
                             "the analyzer must be order-insensitive)")
    p_scan.add_argument("--max-staged-files", type=positive_int, default=1,
                        help="max files kept on scratch at once (default 1 = the "
                             "storage-safe one-file guarantee; raise to trade disk "
                             "for download/analyze overlap, bound = N x largest file)")
    p_scan.add_argument("--max-files", type=positive_int, default=None)
    p_scan.add_argument("--max-frames-per-file", type=positive_int, default=None,
                        help="analyze only the first N frames (FFT windows) of each "
                             "file -- caps per-file work for a fast spot-check "
                             "(note: the whole file is still fetched)")
    p_scan.add_argument("--quarantine", default=None,
                        help="quarantine ledger path (default "
                             "results/<tel>/quarantine.jsonl); bad/unreadable "
                             "files are recorded here and skipped on re-runs")
    p_scan.add_argument("--no-quarantine", action="store_true",
                        help="disable quarantine; treat unreadable files as "
                             "hard (retryable) failures instead")
    p_scan.add_argument("--gpu", action="store_true",
                        help="hint analyzers to use the GPU (exposed as "
                             "ctx.options['gpu']; an analyzer opts in)")
    p_scan.add_argument("--set", dest="set_opts", action="append", metavar="KEY=VALUE",
                        help="plugin-specific parameter passed via ctx.options "
                             "(repeatable), e.g. --set bracket_hz=400")
    p_scan.add_argument("--dry-run", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_setup_cupy = sub.add_parser(
        "setup-cupy",
        help="install the CuPy build matching this image's CUDA (for scan --gpu)")
    p_setup_cupy.add_argument(
        "--install", action="store_true",
        help="pip-install the detected cupy-cudaXXx wheel if the image has no cupy "
             "(otherwise just report what is found)")
    p_setup_cupy.set_defaults(func=cmd_setup_cupy)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    registry.load_plugins(extra=getattr(args, "plugin", None) or [])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())