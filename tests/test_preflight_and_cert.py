#!/usr/bin/env python3
"""
Offline tests for the cadc-datatrail source's host/site resolution and the
survey-prerequisite checks in preflight() -- the pieces that otherwise only
exercise on CANFAR. No CADC or datatrail access: the CANFAR mount, the cert
files, and the datatrail Python API (dtcli.src.functions) are all faked via
monkeypatch.

Run:  PYTHONPATH=src python -m pytest tests/test_preflight_and_cert.py
"""
from __future__ import annotations

import os
import sys
import types

from datatrawl.plugins.sources import cadc_datatrail as src
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
# preflight(): survey prerequisites (private dtcli symbol + scope existence)
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


def _install_fake_dtcli(monkeypatch, with_symbol=True, list_impl=None,
                        ps_impl=None, with_ps=True):
    dtcli = types.ModuleType("dtcli")
    src_mod = types.ModuleType("dtcli.src")
    funcs = types.ModuleType("dtcli.src.functions")
    funcs.list = list_impl or (lambda *a, **k: {})   # callable; body unused unless tested
    if with_symbol:
        funcs.find_dataset_common_path = lambda *a, **k: None
    if with_ps:
        funcs.ps = ps_impl or (lambda *a, **k: (None, None))
    src_mod.functions = funcs
    dtcli.src = src_mod
    monkeypatch.setitem(sys.modules, "dtcli", dtcli)
    monkeypatch.setitem(sys.modules, "dtcli.src", src_mod)
    monkeypatch.setitem(sys.modules, "dtcli.src.functions", funcs)


def test_preflight_silent_without_datatrail(monkeypatch):
    s = _clean_env_source(monkeypatch)
    monkeypatch.setitem(sys.modules, "dtcli", None)   # `import dtcli` -> ImportError
    ok, problems, notes = s.preflight(_ctx())
    assert ok and problems == []


def test_preflight_flags_missing_dtcli_symbol(monkeypatch):
    s = _clean_env_source(monkeypatch)
    _install_fake_dtcli(monkeypatch, with_symbol=False)
    monkeypatch.setattr(src.DATATRAIL, "list_scopes", lambda: list(_Inst.scopes))
    ok, problems, notes = s.preflight(_ctx())
    assert not ok
    assert any("find_dataset_common_path" in p for p in problems)


def test_preflight_flags_unknown_scope(monkeypatch):
    s = _clean_env_source(monkeypatch)
    _install_fake_dtcli(monkeypatch, with_symbol=True)
    # datatrail knows only ONE of the instrument's two configured scopes
    monkeypatch.setattr(src.DATATRAIL, "list_scopes",
                        lambda: ["chime.event.baseband.raw"])
    ok, problems, notes = s.preflight(_ctx())
    assert not ok
    assert any("not found in datatrail" in p
               and "chime.scheduled.baseband.raw" in p for p in problems)


def test_preflight_clean_when_scopes_known(monkeypatch):
    s = _clean_env_source(monkeypatch)
    _install_fake_dtcli(monkeypatch, with_symbol=True)
    monkeypatch.setattr(src.DATATRAIL, "list_scopes", lambda: list(_Inst.scopes))
    ok, problems, notes = s.preflight(_ctx())
    assert ok and problems == []
    assert notes == []          # scopes validated -> no skip note


def test_preflight_validates_scope_override(monkeypatch):
    # an explicit --scope override that datatrail doesn't know must be flagged
    s = _clean_env_source(monkeypatch)
    _install_fake_dtcli(monkeypatch, with_symbol=True)
    monkeypatch.setattr(src.DATATRAIL, "list_scopes", lambda: list(_Inst.scopes))
    ok, problems, notes = s.preflight(_ctx(scope="chime.typo.baseband.raw"))
    assert not ok
    assert any("chime.typo.baseband.raw" in p for p in problems)


def test_preflight_skips_scope_check_when_ls_unreachable(monkeypatch):
    s = _clean_env_source(monkeypatch)
    _install_fake_dtcli(monkeypatch, with_symbol=True)

    def _boom():
        raise RuntimeError("datatrail server unreachable")   # unexpected failure

    monkeypatch.setattr(src.DATATRAIL, "list_scopes", _boom)
    # listing failed -> scope validation SKIPPED (a visible, non-fatal note),
    # NOT reported as 'all invalid'
    ok, problems, notes = s.preflight(_ctx())
    assert ok and problems == []
    assert any("not validated" in n for n in notes)   # the [--] skip note


# ==========================================================================
# listing goes through dtcli.src.functions.list (the Python API, NOT the CLI):
# verify the adapter consumes each result shape and turns an error into []
# ==========================================================================
def test_listing_via_functions_api(monkeypatch):
    calls = {}

    def fake_list(scope=None, dataset=None, **kw):
        calls["last"] = (scope, dataset)
        if scope is None:                       # list scopes
            return {"scopes": ["chime.event.baseband.raw",
                               "chime.scheduled.baseband.raw"]}
        if dataset is None:                     # larger-datasets in a scope
            return {"larger_datasets": ["2023", "2024"]}
        return {"datasets": ["123456789", "987654321"]}   # children (event ids)

    _install_fake_dtcli(monkeypatch, with_symbol=True, list_impl=fake_list)

    assert src.DATATRAIL.list_scopes() == ["chime.event.baseband.raw",
                                           "chime.scheduled.baseband.raw"]
    assert src.DATATRAIL.list_datasets("chime.event.baseband.raw") == ["2023", "2024"]
    assert calls["last"] == ("chime.event.baseband.raw", None)
    assert src.DATATRAIL.events_in_dataset("chime.event.baseband.raw", "2024") == \
        ["123456789", "987654321"]


def test_files_via_functions_ps(monkeypatch):
    """files() = the programmatic `ps -s`: normalization replicates dtcli's
    own (prefix strip, // collapse, commonprefix trimmed to the last /)."""
    day_uris = [
        "cadc:CHIMEFRB/data/gbo/complex_gains/20230530/gain_A_casa.h5",
        "cadc:CHIMEFRB/data/gbo//complex_gains/20230530/gain_B_cyga.h5",
    ]
    _install_fake_dtcli(
        monkeypatch, with_symbol=True,
        ps_impl=lambda *a, **k: (
            {"file_replica_locations": {"minoc": list(day_uris)}}, {"p": 1}))
    cp, names, ok = src.DATATRAIL.files("gbo.acquisition.processed", "20230530")
    assert ok
    assert cp == "cadc:CHIMEFRB/data/gbo/complex_gains/20230530"
    assert names == ["gain_A_casa.h5", "gain_B_cyga.h5"]
    # a fetch URI is exactly path/name -- the same join enumerate uses
    assert f"{cp}/{names[0]}" == day_uris[0]


def test_files_no_minoc_and_outage(monkeypatch):
    _install_fake_dtcli(monkeypatch, ps_impl=lambda *a, **k: (
        {"file_replica_locations": {"arc": ["x"]}}, {}))
    assert src.DATATRAIL.files("s", "d") == (None, [], True)   # answered: no bytes
    def boom(*a, **k):
        raise ConnectionError("Datatrail Server at CHIME is not responding.")
    _install_fake_dtcli(monkeypatch, ps_impl=boom)
    cp, names, ok = src.DATATRAIL.files("s", "d", retries=0)
    assert (cp, names, ok) == (None, [], False)                # outage, not empty


def test_api_available_flags_missing_ps(monkeypatch):
    _install_fake_dtcli(monkeypatch, with_symbol=True, with_ps=False)
    ok, detail = src.Datatrail.api_available()
    assert not ok and "ps" in detail


def test_listing_error_degrades_to_empty(monkeypatch):
    # a datatrail-reported error must yield [] ("couldn't determine"), not raise
    _install_fake_dtcli(
        monkeypatch, with_symbol=True,
        list_impl=lambda *a, **k: {"error": "Server not responding."})
    assert src.DATATRAIL.list_scopes() == []
    assert src.DATATRAIL.list_datasets("s") == []
    assert src.DATATRAIL.events_in_dataset("s", "d") == []


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
