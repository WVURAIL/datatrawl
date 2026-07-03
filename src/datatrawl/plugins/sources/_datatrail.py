"""
datatrawl <-> datatrail boundary -- the single place datatrawl depends on the
CHIME/FRB Datatrail CLI distribution (`datatrail-cli`, import name `dtcli`).

datatrail is a SURVEY-ONLY dependency. survey() uses it to discover the archive
landscape -- which scopes, datasets, and events exist -- and to resolve each
event's CADC common path. The bulk data path (cadcget / cadcinfo) goes straight
to CADC and never touches datatrail, so nothing here is imported on the
scan/fetch path.

We talk to datatrail through its Python API -- the functions in
`dtcli.src.functions` -- NOT by shelling out to the `datatrail` CLI. The CLI's
own commands are thin Click wrappers over exactly these functions: `datatrail ls`
calls `functions.list(...)` and renders the returned dict as a Rich table, and
common-path resolution calls `functions.find_dataset_common_path(...)`. Calling
the functions directly returns structured results (dicts keyed `scopes` /
`larger_datasets` / `datasets`, or `error`) instead of subprocess output we would
have to scrape, and removes a class of environment failures: CLI-not-on-PATH,
user-site isolation, and terminal-width-dependent table truncation.

The one cost is coupling to an internal module: `dtcli.src.functions` is not a
published, stable API (the `src` namespace signals as much). We accept that
deliberately -- scraping the CLI's rendered table coupled us to an even less
stable surface (its exact pretty-printed text) -- and contain it here:
`api_available()` verifies the symbols we call exist, so a datatrail upgrade that
moves or renames one becomes a clean, up-front `doctor` failure rather than a
mid-survey stall.

UPSTREAM NOTE (parked, recorded here so it is not lost):
    The clean long-term fix is a machine-readable mode on the CLI -- e.g.
    `datatrail ls --json` writing the same dict to stdout -- which would give a
    *stable, public* contract and let us drop the internal-module import
    entirely. That needs a PR to CHIMEFRB/datatrail-cli (we have no write
    access), so it is deferred. Until then, the direct `functions.*` calls below
    are the pragmatic choice; if `--json` lands, only the listing methods here
    change, behind unchanged signatures.
"""
from __future__ import annotations

import contextlib
import logging
import re
import sys
import time
from typing import List

# event IDs are embedded in datatrail's child-dataset names; the only parsing we
# still do is pulling those IDs out of the (now structured) name list.
_EVENT_RE = re.compile(r"\b\d{6,}\b")

# the dtcli.src.functions symbols datatrawl calls -- checked by api_available().
_REQUIRED_FUNCS = ("list", "find_dataset_common_path")

# dtcli configures a root RichHandler (dtcli/__init__.py basicConfig) and logs
# through these named loggers. When its config file is absent, config.procure()
# calls log.exception(), dumping a full traceback to the console -- and
# functions.list(quiet=True) only lowers the "functions" logger, never "config",
# so the quiet flag cannot suppress it. We already turn any failure on this path
# into our own one-line stderr message (and a clean doctor "[--]" line), so that
# traceback is pure noise. Silence dtcli's own loggers around each call.
_DTCLI_LOGGERS = ("config", "functions", "dtcli")


@contextlib.contextmanager
def _quiet_dtcli_logging():
    """Mute datatrail-cli's internal loggers for the duration of a call.

    Level alone cannot do this: every dtcli function begins with
    utilities.set_log_level(logger, verbose, quiet), which under quiet=True
    re-arms its own logger to ERROR *inside the call* -- so a pre-set
    CRITICAL+1 is overwritten and dtcli's ERROR records (e.g. 'Service not
    responding.') still hit the root RichHandler, duplicating the one-line
    message our adapter already prints. Blocking propagation (and any
    directly-attached handlers) is outage-proof because set_log_level only
    touches the level. Everything is restored on exit; scoped to dtcli's own
    loggers, so nothing else is affected.
    """
    saved = []
    for n in _DTCLI_LOGGERS:
        lg = logging.getLogger(n)
        saved.append((lg, lg.level, lg.propagate, list(lg.handlers)))
        lg.setLevel(logging.CRITICAL + 1)
        lg.propagate = False
        lg.handlers = []
    try:
        yield
    finally:
        for lg, level, propagate, handlers in saved:
            lg.setLevel(level)
            lg.propagate = propagate
            lg.handlers = handlers


def _functions():
    """Import and return dtcli.src.functions, or raise.

    Callers either guard with installed()/api_available() first, or catch the
    import error and degrade (the listing helpers and common_path do the latter).
    """
    from dtcli.src import functions
    return functions


class Datatrail:
    """Typed adapter over datatrail's Python API (`dtcli.src.functions`).

    Stateless: every method imports + calls per invocation, so one shared instance
    is safe to reuse and to call from the survey verify pool. The listing methods
    degrade to [] on any datatrail-reported error (logged to stderr); callers must
    treat [] as "couldn't determine", never as "definitively empty".
    """

    # -- availability (for doctor / preflight) -----------------------------
    @staticmethod
    def installed() -> bool:
        """True if datatrail-cli (`dtcli`) imports -- i.e. datatrail is present."""
        try:
            import dtcli  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def api_available() -> "tuple[bool, str]":
        """(ok, detail): does dtcli.src.functions expose the symbols we call?

        Contains datatrawl's single internal coupling to datatrail, so doctor can
        report a moved/renamed function before a run rather than survey failing on
        it mid-walk. `detail` names the missing symbol(s) when not ok.
        """
        try:
            functions = _functions()
        except Exception as exc:
            return False, (f"cannot import dtcli.src.functions "
                           f"({type(exc).__name__}: {exc})")
        missing = [n for n in _REQUIRED_FUNCS
                   if not callable(getattr(functions, n, None))]
        if missing:
            return False, f"dtcli.src.functions is missing {missing}"
        return True, ""

    # -- discovery (listing), via functions.list(...) ----------------------
    @staticmethod
    def _list_result_checked(scope=None, dataset=None) -> "tuple[dict, bool]":
        """functions.list(...) -> (dict, ok).

        ok=False means the SERVICE could not answer (exception, or datatrail's
        own {"error": ...}); the dict is then {}. ok=True with an empty dict is
        a genuine "nothing registered here". The distinction exists because a
        discovery walk must never let an outage read as emptiness -- the
        unchecked helpers below collapse both to [] for callers whose contract
        already says "treat [] as couldn't-determine"."""
        try:
            with _quiet_dtcli_logging():
                res = _functions().list(scope, dataset, quiet=True)
        except Exception as exc:
            sys.stderr.write(f"[datatrail list scope={scope} dataset={dataset}] "
                             f"{type(exc).__name__}: {exc}\n")
            return {}, False
        if not isinstance(res, dict):
            return {}, False
        if res.get("error"):
            sys.stderr.write(f"[datatrail list scope={scope} dataset={dataset}] "
                             f"{res['error']}\n")
            return {}, False
        return res, True

    @classmethod
    def _list_result(cls, scope=None, dataset=None) -> dict:
        res, _ok = cls._list_result_checked(scope, dataset)
        return res

    def list_scopes(self) -> List[str]:
        """Every scope datatrail can see (functions.list with no scope)."""
        return self.list_scopes_checked()[0]

    def list_scopes_checked(self) -> "tuple[List[str], bool]":
        """(scopes, ok): ok=False = datatrail did not answer, not "no scopes"."""
        res, ok = self._list_result_checked()
        return list(res.get("scopes", [])), ok

    def list_datasets(self, scope: str) -> List[str]:
        """The larger-datasets registered under one scope."""
        return self.list_datasets_checked(scope)[0]

    def list_datasets_checked(self, scope: str) -> "tuple[List[str], bool]":
        """(datasets, ok): ok=False = the scope could not be LISTED (outage),
        which is not the same map row as a scope with nothing under it."""
        res, ok = self._list_result_checked(scope)
        return list(res.get("larger_datasets", [])), ok

    def children(self, scope: str, dataset: str) -> List[str]:
        """The child dataset names one level under (scope, dataset), verbatim.

        This is the raw name list `events_in_dataset` extracts event IDs from.
        Recon's --expand writes it out unfiltered, because non-event products
        (timestamped acquisitions, calibration containers) have no event ID to
        extract -- their names ARE the handle you resolve with `datatrail ps`.
        Degrades to [] like the other listing methods: treat that as "couldn't
        determine", never "definitively empty".
        """
        return self.children_checked(scope, dataset)[0]

    def children_checked(self, scope: str,
                         dataset: str) -> "tuple[List[str], bool]":
        """(children, ok): the checked form of children() -- recon's --expand
        uses it so an unlistable container is reported as such, not written to
        the map as if it were childless."""
        res, ok = self._list_result_checked(scope, dataset)
        return [str(n) for n in res.get("datasets", [])], ok

    def events_in_dataset(self, scope: str, dataset: str) -> List[str]:
        """Event IDs under one larger-dataset (extracted from child names)."""
        return [ev for name in self.children(scope, dataset)
                for ev in _EVENT_RE.findall(name)]

    # -- common-path resolution --------------------------------------------
    def common_path(self, scope: str, event: str, *, retries: int = 3,
                    base: float = 4.0) -> tuple:
        """Resolve an event's CADC common path via find_dataset_common_path.

        This has always used the library call, never the CLI: `datatrail ps`
        renders a Rich table that wraps/truncates long paths with terminal width,
        so its text can't be parsed reliably; the function returns the path
        straight from the central server as a plain string.

        Contract: (None, False) = couldn't query (transient/service down,
        retried); (None, True) = queried OK but no minoc files (no-data);
        (path, True) = resolved, prefixed with the cadc:CHIMEFRB collection.
        """
        try:
            find_dataset_common_path = _functions().find_dataset_common_path
        except Exception:
            return None, False
        delay = base
        for k in range(retries + 1):
            try:
                with _quiet_dtcli_logging():
                    cp = find_dataset_common_path(scope, event, "", 0, True)
            except Exception:
                cp = None
            if cp is None:                            # queried OK, no minoc files
                return None, True
            if isinstance(cp, str) and " " not in cp:  # a real path (no spaces)
                if not cp.startswith("cadc:"):
                    cp = "cadc:CHIMEFRB/" + cp.lstrip("/")
                return cp, True
            # otherwise cp is the "...Central Server... not reachable!!!" sentence
            if k < retries:
                time.sleep(delay)
                delay *= 2
        return None, False                            # unreachable after retries


# A shared default instance -- survey orchestration and preflight use this.
DATATRAIL = Datatrail()
