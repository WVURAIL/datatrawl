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
_DTCLI_LOGGERS = ("config", "functions")


@contextlib.contextmanager
def _quiet_dtcli_logging():
    """Mute datatrail-cli's internal loggers for the duration of a call, then
    restore their levels. Scoped to dtcli's own loggers, so nothing else is
    affected."""
    saved = [(lg := logging.getLogger(n), lg.level) for n in _DTCLI_LOGGERS]
    try:
        for lg, _ in saved:
            lg.setLevel(logging.CRITICAL + 1)
        yield
    finally:
        for lg, level in saved:
            lg.setLevel(level)


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
    def _list_result(scope=None, dataset=None) -> dict:
        """functions.list(...) -> dict, with any datatrail-reported error turned
        into {} at the call site (logged to stderr, never raised)."""
        try:
            with _quiet_dtcli_logging():
                res = _functions().list(scope, dataset, quiet=True)
        except Exception as exc:
            sys.stderr.write(f"[datatrail list scope={scope} dataset={dataset}] "
                             f"{type(exc).__name__}: {exc}\n")
            return {}
        if not isinstance(res, dict):
            return {}
        if res.get("error"):
            sys.stderr.write(f"[datatrail list scope={scope} dataset={dataset}] "
                             f"{res['error']}\n")
            return {}
        return res

    def list_scopes(self) -> List[str]:
        """Every scope datatrail can see (functions.list with no scope)."""
        return list(self._list_result().get("scopes", []))

    def list_datasets(self, scope: str) -> List[str]:
        """The larger-datasets registered under one scope."""
        return list(self._list_result(scope).get("larger_datasets", []))

    def events_in_dataset(self, scope: str, dataset: str) -> List[str]:
        """Event IDs under one larger-dataset (extracted from child names)."""
        children = self._list_result(scope, dataset).get("datasets", [])
        return [ev for name in children for ev in _EVENT_RE.findall(str(name))]

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
