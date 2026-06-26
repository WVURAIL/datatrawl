#!/usr/bin/env python3
"""
CLI registration smoke test.

Runs `datatrawl list` / `doctor` in a fresh interpreter and checks that exactly
the real plugins register and the discovery commands stay healthy. This is a guard
for the cleanup: it fails loudly if a removed concept (a template plugin, the old
data-product source) ever creeps back, or if a registration starts raising.

Run:  PYTHONPATH=src python tests/test_cli_smoke.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def _run(*argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "datatrawl.cli", *argv],
                          capture_output=True, text=True, env=env)


# the full set of shipped plugins -- update this when one is genuinely added/removed
EXPECTED = {"cadc-datatrail", "local", "chime-baseband", "spectrum"}
# concepts that were removed and must never reappear in the listing
FORBIDDEN = {"template-", "datatrail-product", "data-product"}


def test_list_shows_only_real_plugins():
    r = _run("list")
    assert r.returncode == 0, f"`list` failed:\n{r.stderr[-400:]}"
    out = r.stdout
    for name in EXPECTED:
        assert name in out, f"`list` is missing the {name!r} plugin"
    for bad in FORBIDDEN:
        assert bad not in out, f"`list` still shows a removed concept: {bad!r}"


def test_version_flag_reports_installed_version():
    r = _run("--version")
    assert r.returncode == 0
    assert r.stdout.startswith("datatrawl ")


def test_doctor_runs_without_error():
    r = _run("doctor")
    # doctor returns 0 (all combos ready) or 1 (some prereqs missing); either is a
    # clean run. What must NOT happen is an unhandled exception.
    assert r.returncode in (0, 1), f"unexpected doctor exit {r.returncode}"
    assert "Traceback" not in (r.stdout + r.stderr), \
        f"doctor raised:\n{(r.stdout + r.stderr)[-500:]}"


def test_doctor_full_combo_reports_telescope_readiness():
    # regression guard for the doctor --telescope Readiness check (a past crash)
    r = _run("doctor", "--telescope", "chime", "--source", "cadc-datatrail",
             "--reader", "chime-baseband", "--analyzer", "spectrum")
    assert "Traceback" not in (r.stdout + r.stderr)
    assert "Nyquist zone set" in r.stdout       # the telescope check actually ran


def test_doctor_and_explore_accept_plugin_set_options():
    import datatrawl.cli as cli

    parser = cli.build_parser()
    for command in ("doctor", "explore"):
        args = parser.parse_args(
            [command, "--set", "threshold=3.5", "--set", "enabled=true"]
        )
        options = cli._parse_set_options(args.set_opts)
        assert options["threshold"] == 3.5
        assert options["enabled"] is True


def test_survey_reports_on_demand_source_cleanly():
    r = _run("survey", "--telescope", "chime", "--source", "local")
    text = r.stdout + r.stderr
    assert r.returncode == 2
    assert "enumerates on demand" in text
    assert "Traceback" not in text


def test_auto_tmp_dirs_are_portable_unique_and_cleanup_safe(monkeypatch, tmp_path):
    import datatrawl.cli as cli

    monkeypatch.setenv("DATATRAWL_TMPDIR", str(tmp_path))
    args = SimpleNamespace(tmp_dir=None, analyzer="spectrum")
    instrument = cli.inst_mod.load_instrument("chime")

    d1, auto1 = cli._scan_tmp_dir(args, instrument)
    d2, auto2 = cli._scan_tmp_dir(args, instrument)
    try:
        assert auto1 and auto2
        assert d1 != d2
        assert os.path.dirname(d1) == str(tmp_path)
        assert os.path.dirname(d2) == str(tmp_path)
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)


def test_default_quarantine_path_is_source_reader_scoped(tmp_path):
    import datatrawl.cli as cli

    args = SimpleNamespace(
        root=str(tmp_path), source="local", reader="chime-baseband"
    )
    instrument = cli.inst_mod.load_instrument("chime")
    path = cli._default_quarantine_path(args, instrument)
    assert path.endswith(
        os.path.join("chime", "quarantine", "local--chime-baseband.jsonl")
    )


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print(f"  ok: {fn}")
    print("CLI SMOKE TESTS PASSED")
