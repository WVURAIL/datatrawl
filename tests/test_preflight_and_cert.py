#!/usr/bin/env python3
"""
Offline tests for the cadc-datatrail source's host/site resolution and the
survey-prerequisite checks in preflight() -- the pieces that otherwise only
exercise on CANFAR. No CADC or datatrail access: the CANFAR mount, the cert
files, and the datatrail CLI's --json boundary are all faked -- the last at
subprocess.run, so every test also locks the invocation contract itself
(same-interpreter `-m dtcli.cli`, trailing `--json`, banner-tolerant parse).

Run:  PYTHONPATH=src python -m pytest tests/test_preflight_and_cert.py
"""
from __future__ import annotations

import json
import os
import sys
import types

from datatrawl.plugins.sources import cadc_datatrail as src
from datatrawl.plugins.sources import _datatrail as dt
from datatrawl.interfaces import RunContext, PluginInfo, READY
from datatrawl import cli, registry


# ==========================================================================
# _default_cert(): explicit CADC_CERT wins, else the personal ~/.ssl default
# ==========================================================================
def test_cert_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("CADC_CERT", "/somewhere/explicit.pem")
    # explicit is honoured even when it doesn't exist (the user's stated intent)
    monkeypatch.setattr(src.os.path, "exists", lambda p: False)
    assert src._default_cert() == "/somewhere/explicit.pem"


def test_cert_defaults_to_personal_when_no_env(monkeypatch):
    # no CADC_CERT -> the personal ~/.ssl path (preflight checks it exists)
    monkeypatch.delenv("CADC_CERT", raising=False)
    assert src._default_cert() == os.path.expanduser("~/.ssl/cadcproxy.pem")


# ==========================================================================
# the fake datatrail CLI: routes the adapter's one subprocess boundary into a
# handler(args) -> (returncode, stdout, stderr), where args excludes the
# [sys.executable, -m, dtcli.cli] prefix and the trailing --json -- both of
# which the fake asserts, so a drifting invocation fails loudly here.
# ==========================================================================
class _Proc:
    def __init__(self, rc, out, err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _install_fake_cli(monkeypatch, handler=None, version=(0, 11, 0)):
    """datatrail "installed" at `version`; CLI calls answered by `handler`.

    handler=None means the test expects NO datatrail subprocess at all --
    any call trips an assertion (preflight fakes list_scopes above this
    boundary, so it must never spawn).
    """
    monkeypatch.setitem(sys.modules, "dtcli", types.ModuleType("dtcli"))
    monkeypatch.setattr(dt, "_cli_version", lambda: version)

    def fake_run(cmd, **kw):
        assert handler is not None, f"unexpected datatrail call: {cmd}"
        assert cmd[:3] == [sys.executable, "-m", "dtcli.cli"], cmd
        assert cmd[-1] == "--json", cmd
        rc, out, err = handler(cmd[3:-1])
        return _Proc(rc, out, err)

    monkeypatch.setattr(dt.subprocess, "run", fake_run)


# dtcli's group callback prints this to STDOUT ahead of any command's output
# when PyPI shows a newer release -- the parse must skip it (see _extract_json).
_BANNER = "A new release of datatrail-cli is available: 0.11.0 \u2192 0.12.0\n\n"


def _ps_payload(files):
    return json.dumps(
        {"dataset": "d", "scope": "s", "files": files, "policies": {"p": 1}})


# ==========================================================================
# preflight(): survey prerequisites (CLI --json contract + scope existence)
# ==========================================================================
class _Inst:
    name = "chime"
    scopes = ("chime.event.baseband.raw", "chime.scheduled.baseband.raw")


def _ctx(scope=None):
    return RunContext(instrument=_Inst(),
                      options=({"scope": scope} if scope is not None else {}))


def _clean_env_source(monkeypatch):
    """A source whose cert + cadc checks pass, to isolate the datatrail checks."""
    s = src.CadcDatatrailSource()
    monkeypatch.setattr(src.os.path, "exists", lambda p: True)   # cert "present"
    monkeypatch.setitem(sys.modules, "cadcdata", types.ModuleType("cadcdata"))
    monkeypatch.setitem(sys.modules, "cadcutils", types.ModuleType("cadcutils"))
    return s


def test_preflight_silent_without_datatrail(monkeypatch):
    s = _clean_env_source(monkeypatch)
    monkeypatch.setitem(sys.modules, "dtcli", None)   # `import dtcli` -> ImportError
    ok, problems, notes = s.preflight(_ctx())
    assert ok and problems == []


def test_preflight_flags_pre_json_datatrail(monkeypatch):
    # datatrail present but older than the --json contract (< 0.11): the check
    # must name the real cause up front, not let survey misread it mid-walk
    s = _clean_env_source(monkeypatch)
    _install_fake_cli(monkeypatch, version=(0, 10, 3))
    monkeypatch.setattr(src.DATATRAIL, "list_scopes", lambda: list(_Inst.scopes))
    ok, problems, notes = s.preflight(_ctx())
    assert not ok
    assert any("--json" in p and "0.11" in p for p in problems)


def test_preflight_flags_unknown_scope(monkeypatch):
    s = _clean_env_source(monkeypatch)
    _install_fake_cli(monkeypatch)
    # datatrail knows only ONE of the instrument's two configured scopes
    monkeypatch.setattr(src.DATATRAIL, "list_scopes",
                        lambda: ["chime.event.baseband.raw"])
    ok, problems, notes = s.preflight(_ctx())
    assert not ok
    assert any("not found in datatrail" in p
               and "chime.scheduled.baseband.raw" in p for p in problems)


def test_preflight_clean_when_scopes_known(monkeypatch):
    s = _clean_env_source(monkeypatch)
    _install_fake_cli(monkeypatch)
    monkeypatch.setattr(src.DATATRAIL, "list_scopes", lambda: list(_Inst.scopes))
    ok, problems, notes = s.preflight(_ctx())
    assert ok and problems == []
    assert notes == []          # scopes validated -> no skip note


def test_preflight_validates_scope_override(monkeypatch):
    # an explicit --scope override that datatrail doesn't know must be flagged
    s = _clean_env_source(monkeypatch)
    _install_fake_cli(monkeypatch)
    monkeypatch.setattr(src.DATATRAIL, "list_scopes", lambda: list(_Inst.scopes))
    ok, problems, notes = s.preflight(_ctx(scope="chime.typo.baseband.raw"))
    assert not ok
    assert any("chime.typo.baseband.raw" in p for p in problems)


def test_preflight_skips_scope_check_when_ls_unreachable(monkeypatch):
    s = _clean_env_source(monkeypatch)
    _install_fake_cli(monkeypatch)

    def _boom():
        raise RuntimeError("datatrail server unreachable")   # unexpected failure

    monkeypatch.setattr(src.DATATRAIL, "list_scopes", _boom)
    # listing failed -> scope validation SKIPPED (a visible, non-fatal note),
    # NOT reported as 'all invalid'
    ok, problems, notes = s.preflight(_ctx())
    assert ok and problems == []
    assert any("not validated" in n for n in notes)   # the [--] skip note


# ==========================================================================
# api_available(): the doctor-time gate is now a version check -- the coupling
# is the public `--json` flag (datatrail-cli >= 0.11), not an internal symbol
# ==========================================================================
def test_api_available_ok_at_contract_version(monkeypatch):
    _install_fake_cli(monkeypatch, version=(0, 11, 0))
    ok, detail = src.Datatrail.api_available()
    assert ok and detail == ""


def test_api_available_flags_pre_json_cli(monkeypatch):
    _install_fake_cli(monkeypatch, version=(0, 10, 3))
    ok, detail = src.Datatrail.api_available()
    assert not ok and "0.11" in detail and "--json" in detail


def test_api_available_flags_unknown_version(monkeypatch):
    _install_fake_cli(monkeypatch, version=None)
    ok, detail = src.Datatrail.api_available()
    assert not ok and "version" in detail


# ==========================================================================
# listing goes through `datatrail ls --json` (the public contract, NOT
# dtcli internals): verify the adapter consumes each payload shape, parses
# past the update banner, and turns any not-answered case into []
# ==========================================================================
def test_listing_via_ls_json(monkeypatch):
    def handler(args):
        if args == ["ls"]:                       # list scopes (+ banner noise)
            return 0, _BANNER + json.dumps(
                {"scopes": ["chime.event.baseband.raw",
                            "chime.scheduled.baseband.raw"]}), ""
        if args == ["ls", "chime.event.baseband.raw"]:
            return 0, json.dumps({"larger_datasets": ["2023", "2024"]}), ""
        if args == ["ls", "chime.event.baseband.raw", "2024"]:
            return 0, json.dumps(
                {"datasets": ["123456789", "987654321"]}), ""
        raise AssertionError(f"unexpected ls args: {args}")

    _install_fake_cli(monkeypatch, handler)

    assert src.DATATRAIL.list_scopes() == ["chime.event.baseband.raw",
                                           "chime.scheduled.baseband.raw"]
    assert src.DATATRAIL.list_datasets("chime.event.baseband.raw") == ["2023", "2024"]
    assert src.DATATRAIL.events_in_dataset("chime.event.baseband.raw", "2024") == \
        ["123456789", "987654321"]


def test_listing_error_degrades_to_empty(monkeypatch):
    # a datatrail-reported error (JSON {"error": ...}, exit 1) must yield []
    # ("couldn't determine"), not raise
    _install_fake_cli(monkeypatch, lambda a: (
        1, json.dumps({"error": "Server not responding."}), ""))
    assert src.DATATRAIL.list_scopes() == []
    assert src.DATATRAIL.list_datasets("s") == []
    assert src.DATATRAIL.events_in_dataset("s", "d") == []


def test_listing_nonjson_stdout_is_not_answered(monkeypatch):
    # the CLI's invalid-scope path prints a Rich scopes table (exit 0) instead
    # of JSON; that must read as "did not answer", never as an empty scope
    table = "\u250f\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2513\n" \
            "\u2503 Scopes \u2503\n"
    _install_fake_cli(monkeypatch, lambda a: (0, table, "Scope does not exist!"))
    datasets, ok = src.DATATRAIL.list_datasets_checked("chime.typo")
    assert (datasets, ok) == ([], False)


def test_listing_shape_drift_is_not_answered(monkeypatch):
    # a parsed non-error dict MISSING the key this arity is defined to return
    # (a changed contract in a newer datatrail-cli -- the pin has no ceiling)
    # must read as not-answered, never as a scope with nothing under it
    _install_fake_cli(monkeypatch, lambda a: (
        0, json.dumps({"renamed_in_0_12": []}), ""))
    assert src.DATATRAIL.list_scopes_checked() == ([], False)
    assert src.DATATRAIL.list_datasets_checked("s") == ([], False)
    assert src.DATATRAIL.children_checked("s", "d") == ([], False)
    # while the key PRESENT with an empty list stays a genuine "nothing
    # registered here" -- the row recon is allowed to write to the map
    _install_fake_cli(monkeypatch, lambda a: (
        0, json.dumps({"larger_datasets": []}), ""))
    assert src.DATATRAIL.list_datasets_checked("s") == ([], True)


def test_listing_nonlist_value_is_not_answered(monkeypatch):
    # captured live from dtcli 0.11.0 behind a blocking proxy: the scopes
    # endpoint's error text leaked through as the VALUE, with exit 0 --
    # naive list() would shred it into per-character "scopes"
    _install_fake_cli(monkeypatch, lambda a: (0, json.dumps(
        {"scopes": "Host not in allowlist: frb.chimenet.ca."}), ""))
    assert src.DATATRAIL.list_scopes_checked() == ([], False)
    _install_fake_cli(monkeypatch, lambda a: (
        0, json.dumps({"datasets": "oops"}), ""))
    assert src.DATATRAIL.children_checked("s", "d") == ([], False)


# ==========================================================================
# files() / common_path() go through `datatrail ps --json`: verify each
# payload shape maps onto the unchanged outage-vs-empty contract
# ==========================================================================
def test_files_via_ps_json(monkeypatch):
    """files() = the programmatic `ps --json`: normalization replicates dtcli's
    own (prefix strip, // collapse, commonprefix trimmed to the last /)."""
    day_uris = [
        "cadc:CHIMEFRB/data/gbo/complex_gains/20230530/gain_A_casa.h5",
        "cadc:CHIMEFRB/data/gbo//complex_gains/20230530/gain_B_cyga.h5",
    ]

    def handler(args):
        assert args == ["ps", "gbo.acquisition.processed", "20230530"], args
        return 0, _ps_payload(
            {"file_replica_locations": {"minoc": list(day_uris)}}), ""

    _install_fake_cli(monkeypatch, handler)
    cp, names, ok = src.DATATRAIL.files("gbo.acquisition.processed", "20230530")
    assert ok
    assert cp == "cadc:CHIMEFRB/data/gbo/complex_gains/20230530"
    assert names == ["gain_A_casa.h5", "gain_B_cyga.h5"]
    # a fetch URI is exactly path/name -- the same join enumerate uses
    assert f"{cp}/{names[0]}" == day_uris[0]


def test_files_no_minoc_null_and_outage(monkeypatch):
    # answered, minoc absent -> no bytes (ok=True)
    _install_fake_cli(monkeypatch, lambda a: (
        0, _ps_payload({"file_replica_locations": {"arc": ["x"]}}), ""))
    assert src.DATATRAIL.files("s", "d") == (None, [], True)
    # "files": null is the CLI rendering of functions.ps -> (None, policy):
    # the find half had no answer for this name -- still a no-data verdict
    _install_fake_cli(monkeypatch, lambda a: (0, _ps_payload(None), ""))
    assert src.DATATRAIL.files("s", "d") == (None, [], True)
    # the error envelope (exit 1) is an outage: retried, never "empty"
    _install_fake_cli(monkeypatch, lambda a: (1, json.dumps(
        {"error": {"files": "boom", "policies": "boom"}}), ""))
    assert src.DATATRAIL.files("s", "d", retries=0) == (None, [], False)


def test_files_shape_drift_is_outage_without_retry(monkeypatch):
    # a success payload without the "files" key (0.11 always emits it, null
    # or not) is schema drift: never "dataset has no files", and -- being
    # deterministic -- returned immediately, not retried through backoff
    calls = []

    def handler(args):
        calls.append(args)
        return 0, json.dumps({"dataset": "d", "scope": "s",
                              "policies": {}}), ""

    _install_fake_cli(monkeypatch, handler)
    assert src.DATATRAIL.files("s", "d", retries=3) == (None, [], False)
    assert len(calls) == 1


def test_files_nonnull_nondict_value_is_outage(monkeypatch):
    # a string "files" value is degradation or drift (0.11 wraps string
    # halves in the error envelope, so this shape is unowned): must never
    # read as the no-data verdict that "files": null legitimately carries
    calls = []

    def handler(args):
        calls.append(args)
        return 0, json.dumps({"dataset": "d", "scope": "s",
                              "files": "Server hiccup", "policies": {}}), ""

    _install_fake_cli(monkeypatch, handler)
    assert src.DATATRAIL.files("s", "d", retries=3) == (None, [], False)
    assert len(calls) == 1


def test_files_spawn_failure_is_outage(monkeypatch):
    def handler(args):
        raise OSError("cannot spawn")
    _install_fake_cli(monkeypatch, handler)
    assert src.DATATRAIL.files("s", "d", retries=0) == (None, [], False)


def test_common_path_shares_ps_contract(monkeypatch):
    uris = ["cadc:CHIMEFRB/data/chime/baseband/raw/2024/01/02/astro_303939671/"
            "baseband_303939671_100.h5"]
    _install_fake_cli(monkeypatch, lambda a: (
        0, _ps_payload({"file_replica_locations": {"minoc": uris}}), ""))
    cp, ok = src.DATATRAIL.common_path("chime.event.baseband.raw", "303939671")
    assert ok and cp == ("cadc:CHIMEFRB/data/chime/baseband/raw/2024/01/02/"
                         "astro_303939671")
    _install_fake_cli(monkeypatch, lambda a: (0, _ps_payload(None), ""))
    assert src.DATATRAIL.common_path("s", "e") == (None, True)   # no-data
    _install_fake_cli(monkeypatch, lambda a: (1, json.dumps(
        {"error": "down"}), ""))
    assert src.DATATRAIL.common_path("s", "e", retries=0) == (None, False)


# ==========================================================================
# doctor rendering: a preflight that returns notes shows a visible [--] line
# and reports "skipped" up to the summary, without failing readiness
# ==========================================================================
def test_doctor_renders_skip_note(monkeypatch, capsys):
    class _FakeSource:
        info = PluginInfo(name="fake-src", kind="source", summary="x", status=READY)

        def preflight(self, ctx):
            return True, [], ["datatrail scope(s) not validated: unreachable"]

    monkeypatch.setattr(registry, "get", lambda kind, name: _FakeSource)
    ok, skipped = cli._doctor_plugin("source", "fake-src", ctx=object())
    out = capsys.readouterr().out
    assert ok is True and skipped is True          # non-fatal, but flagged
    assert "[--]" in out and "not validated" in out


def test_doctor_no_skip_when_clean(monkeypatch, capsys):
    class _FakeSource:
        info = PluginInfo(name="fake-src", kind="source", summary="x", status=READY)

        def preflight(self, ctx):
            return True, []                        # legacy 2-tuple still works

    monkeypatch.setattr(registry, "get", lambda kind, name: _FakeSource)
    ok, skipped = cli._doctor_plugin("source", "fake-src", ctx=object())
    out = capsys.readouterr().out
    assert ok is True and skipped is False
    assert "[--]" not in out
