"""
datatrawl <-> datatrail boundary -- the single place datatrawl depends on the
CHIME/FRB Datatrail CLI distribution (`datatrail-cli`).

datatrail is a SURVEY-ONLY dependency. survey() uses it to discover the archive
landscape -- which scopes, datasets, and events exist -- and to resolve each
event's CADC common path. The bulk data path (cadcget / cadcinfo) goes straight
to CADC and never touches datatrail, so nothing here runs on the scan/fetch
path.

We talk to datatrail through the CLI's machine-readable mode -- `datatrail ls
--json` and `datatrail ps --json` -- the stable, public contract added upstream
in datatrail-cli 0.11.0 (CHIMEFRB/datatrail-cli#160). That PR resolves the
UPSTREAM NOTE this module carried through 1.0.0: earlier releases had to choose
between scraping the CLI's Rich tables (terminal-width-dependent text) and
importing `dtcli.src.functions` (internal, unversioned); we shipped the latter,
pinned `<0.11`. With `--json`, both couplings are gone. The payloads are the
same dicts the internal functions returned -- `ls` prints the
`scopes` / `larger_datasets` / `datasets` / `error` dict verbatim (exit 1 when
`error` is present), and `ps` wraps the files+policies pair as
`{"dataset", "scope", "files", "policies"}` or an `{"error": ...}` envelope
with exit 1 -- so every adapter method keeps its signature and its
outage-vs-empty contract; only the transport underneath changed.

Invocation notes, so the failure classes the old in-process calls avoided
stay avoided:
  * The child is `sys.executable -m dtcli.cli`, never a `datatrail` looked up
    on PATH: the interpreter that imports datatrawl is the one that runs
    dtcli, so CLI-not-on-PATH and user-site isolation cannot bite.
  * dtcli's group callback prints an update-available banner to STDOUT before
    any command when PyPI shows a newer release; _extract_json() parses past
    any such preamble.
  * The child inherits the environment plus NO_COLOR=1 / TERM=dumb / PAGER=cat,
    so captured output is plain text and can never block on a pager.
  * Every call carries a hard timeout (DATATRAWL_DATATRAIL_TIMEOUT, default
    300 s); a wedged child is killed and read as an outage instead of stalling
    a survey worker forever.

One deliberate tightening vs. the internal-API era: partial server
degradations that functions.ps() surfaced as odd shapes (a files or policies
half decoded to a bare string) used to fall through as "queried OK, no files".
The CLI reports those as an error envelope with exit 1, and this adapter maps
that to ok=False -- an outage, retried, never mistaken for emptiness -- which
is the contract every caller was already written against.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import List, Optional, Sequence, Tuple

# event IDs are embedded in datatrail's child-dataset names; the only parsing
# we still do is pulling those IDs out of the (structured) name list.
_EVENT_RE = re.compile(r"\b\d{6,}\b")

# the public contract this adapter drives -- `datatrail ls/ps --json` -- landed
# in datatrail-cli 0.11.0. Checked by api_available() so doctor reports an old
# install before a run rather than survey misreading it as an outage mid-walk.
_MIN_CLI = (0, 11)

# hard per-call child timeout. The in-process calls this replaces could hang
# on a wedged HTTP request too; the subprocess boundary lets us actually
# bound it. Timeout kills the child and reads as an outage (retried).
_TIMEOUT_S = float(os.environ.get("DATATRAWL_DATATRAIL_TIMEOUT", "300"))


def _cli_version() -> "Optional[Tuple[int, ...]]":
    """Installed datatrail-cli version as an int tuple, or None if unknown."""
    try:
        from importlib.metadata import version
        raw = version("datatrail-cli")
    except Exception:
        return None
    parts = re.findall(r"\d+", raw)
    return tuple(int(p) for p in parts[:3]) if parts else None


def _extract_json(stdout: str) -> "Optional[dict]":
    """The JSON object in a --json invocation's stdout, or None.

    The payload is not always at offset 0 -- dtcli prints its update-available
    banner to stdout ahead of the command output -- so parse from the first
    '{'. Every --json payload is a single object; stdout without one (empty,
    or the Rich scopes table the invalid-scope path renders) yields None,
    which callers treat as "the CLI did not answer".
    """
    i = stdout.find("{")
    if i < 0:
        return None
    try:
        obj = json.loads(stdout[i:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _run_json(args: "Sequence[str]") -> "tuple[Optional[dict], str]":
    """Run `datatrail <args> --json`; (payload, diag).

    payload=None means the CLI did not answer with JSON -- spawn failure,
    timeout (child killed), or non-JSON stdout -- and diag says why, one line.
    Service-level errors ARE JSON ({"error": ...} with exit 1) and come back
    as a payload for the caller to classify. Never raises.
    """
    cmd = [sys.executable, "-m", "dtcli.cli", *args, "--json"]
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", PAGER="cat")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TIMEOUT_S, env=env)
    except Exception as exc:                      # spawn failure / TimeoutExpired
        return None, f"{type(exc).__name__}: {exc}"
    payload = _extract_json(proc.stdout)
    if payload is None:
        tail = (proc.stderr or "").strip().splitlines()
        return None, (f"exit {proc.returncode}, no JSON on stdout"
                      + (f" ({tail[-1]})" if tail else ""))
    return payload, ""


class Datatrail:
    """Typed adapter over datatrail's machine-readable CLI (`--json`).

    Stateless: every method spawns one `datatrail` child per invocation, so one
    shared instance is safe to reuse and to call from the survey verify pool.
    The listing methods degrade to [] on any datatrail-reported error (logged
    to stderr); callers must treat [] as "couldn't determine", never as
    "definitively empty".
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
        """(ok, detail): does the installed datatrail speak the JSON contract?

        The coupling is a public CLI flag now instead of an internal module,
        but the doctor-time check survives for the same reason it existed: a
        pre-0.11 datatrail-cli (no --json) would misread as a service outage
        an hour into a survey; report the real cause up front instead.
        `detail` says what to install when not ok.
        """
        v = _cli_version()
        if v is None:
            return False, ("cannot determine the datatrail-cli version "
                           "(is datatrail-cli installed in this environment?)")
        if v < _MIN_CLI:
            return False, (f"datatrail-cli {'.'.join(map(str, v))} predates "
                           f"the --json machine-readable mode; install "
                           f"datatrail-cli>=0.11")
        return True, ""

    # -- discovery (listing), via `datatrail ls --json` ---------------------
    @staticmethod
    def _list_result_checked(scope=None, dataset=None) -> "tuple[dict, bool]":
        """`datatrail ls [scope [dataset]] --json` -> (dict, ok).

        ok=False means the SERVICE could not answer (no JSON came back, or
        datatrail's own {"error": ...}); the dict is then {}. ok=True with an
        empty dict is a genuine "nothing registered here". The distinction
        exists because a discovery walk must never let an outage read as
        emptiness -- the unchecked helpers below collapse both to [] for
        callers whose contract already says "treat [] as couldn't-determine".

        Shape guard: a non-error answer must carry the key this arity is
        defined to return (`scopes` / `larger_datasets` / `datasets` -- dtcli
        constructs the first and third, the server the second, and 0.11 emits
        the key even when its list is empty). A parsed dict WITHOUT it is a
        contract change in a newer datatrail-cli, and with an open-ended
        version pin it must read as not-answered, never as an empty scope."""
        args = ["ls"] + [a for a in (scope, dataset) if a]
        key = "datasets" if dataset else ("larger_datasets" if scope
                                          else "scopes")
        payload, diag = _run_json(args)
        if payload is None:
            sys.stderr.write(f"[datatrail ls scope={scope} dataset={dataset}] "
                             f"{diag}\n")
            return {}, False
        if payload.get("error"):
            sys.stderr.write(f"[datatrail ls scope={scope} dataset={dataset}] "
                             f"{payload['error']}\n")
            return {}, False
        if key not in payload:
            sys.stderr.write(
                f"[datatrail ls scope={scope} dataset={dataset}] unexpected "
                f"--json shape (no {key!r} key) -- datatrail-cli newer than "
                f"this adapter understands?\n")
            return {}, False
        if not isinstance(payload[key], list):
            # Observed live: with the scopes endpoint unreachable behind a
            # proxy, dtcli 0.11.0 leaked the error text through as
            # {"scopes": "<proxy error prose>"} with exit 0 (decode_response
            # passes non-JSON bodies along as str). list("<str>") would
            # shred that into per-character "scopes" -- classify as
            # not-answered instead.
            sys.stderr.write(
                f"[datatrail ls scope={scope} dataset={dataset}] non-list "
                f"{key!r} value ({type(payload[key]).__name__}) -- treating "
                f"as not answered\n")
            return {}, False
        return payload, True

    @classmethod
    def _list_result(cls, scope=None, dataset=None) -> dict:
        res, _ok = cls._list_result_checked(scope, dataset)
        return res

    def list_scopes(self) -> List[str]:
        """Every scope datatrail can see (`datatrail ls` with no scope)."""
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

    # -- file listing (`datatrail ps --json`, normalized) --------------------
    def files(self, scope: str, dataset: str, *, retries: int = 3,
              base: float = 4.0) -> tuple:
        """(common_path, [names], ok) for one dataset's minoc files.

        This is `datatrail ps <scope> <dataset> --json`, normalized to the minoc
        replica list. Exposed here so an analyzer resolving a per-day
        companion (a gains day-dataset, say) never scrapes the Rich table or
        imports dtcli internals itself -- this adapter is chartered as the ONE
        place datatrawl touches datatrail.

        Contract mirrors common_path():
          (None, [], False)  = the CLI/service did not answer (retried) --
                               includes the {"error": ...} envelope (exit 1),
                               and (unretried -- it is deterministic, not
                               transient) a success payload MISSING the
                               "files" key, which 0.11 always emits: that is
                               schema drift in a newer CLI, never no-data;
          (None, [], True)   = queried OK, no minoc files for this dataset
                               ("files" null, or without minoc replicas);
          (path, names, True) = resolved. `path` is prefixed cadc:CHIMEFRB/
                                like common_path(); `names` are relative to
                                it, in server order, so a fetch URI is
                                f"{path}/{name}".

        Normalization is unchanged from the internal-API era (and matches
        dtcli's own find_dataset_common_path): strip the cadc:CHIMEFRB/
        prefix, collapse '//', commonprefix trimmed to the last '/'.
        """
        delay = base
        for k in range(retries + 1):
            payload, _diag = _run_json(["ps", scope, dataset])
            if payload is None or "error" in payload:
                if k < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                return None, [], False        # did not answer, retried out
            if "files" not in payload:
                # 0.11's success payload always carries the key ("files" may
                # be null, but it is present). A non-error dict without it is
                # a changed contract in a newer datatrail-cli -- deterministic,
                # so no retry -- and must never read as "dataset has no files".
                sys.stderr.write(f"[datatrail ps {scope} {dataset}] unexpected"
                                 f" --json shape (no 'files' key) -- "
                                 f"datatrail-cli newer than this adapter "
                                 f"understands?\n")
                return None, [], False
            files_resp = payload["files"]
            # "files": null is the CLI's rendering of functions.ps returning
            # (None, policy) -- the find half had no answer for this name
            # while policies resolved. For a dataset the map says exists,
            # that reads as "no files", same as the no-minoc case below.
            # Any OTHER non-dict value (a string, say -- 0.11 wraps string
            # halves in the error envelope, so this shape is unowned) is
            # degradation or drift, never a no-data verdict.
            if files_resp is not None and not isinstance(files_resp, dict):
                sys.stderr.write(f"[datatrail ps {scope} {dataset}] non-dict "
                                 f"'files' value "
                                 f"({type(files_resp).__name__}) -- treating "
                                 f"as not answered\n")
                return None, [], False
            if files_resp is None:
                return None, [], True
            uris = (files_resp.get("file_replica_locations") or {}).get("minoc")
            if not uris:
                return None, [], True
            paths = [str(u).replace("//", "/").replace("cadc:CHIMEFRB/", "")
                     for u in uris]
            common = os.path.commonprefix(paths)
            if not common.endswith("/"):
                common = "/".join(common.split("/")[:-1])
            common = common.rstrip("/")
            names = [p[len(common):].lstrip("/") for p in paths]
            return f"cadc:CHIMEFRB/{common.lstrip('/')}", names, True
        return None, [], False

    # -- common-path resolution --------------------------------------------
    def common_path(self, scope: str, event: str, *, retries: int = 3,
                    base: float = 4.0) -> tuple:
        """Resolve an event's CADC common path.

        Same `ps --json` call as files(), normalized to the path. datatrail
        derives its common path from the minoc URI list and files() replicates
        that normalization exactly, so this equals what dtcli's own
        find_dataset_common_path computed in the internal-API era -- from the
        same /query/dataset/find response.

        Contract (unchanged): (None, False) = couldn't query (transient /
        service down, retried); (None, True) = queried OK but no minoc files
        (no-data); (path, True) = resolved, prefixed with the cadc:CHIMEFRB
        collection.
        """
        cp, _names, ok = self.files(scope, event, retries=retries, base=base)
        return cp, ok


# A shared default instance -- survey orchestration and preflight use this.
DATATRAIL = Datatrail()
