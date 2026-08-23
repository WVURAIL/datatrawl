#!/usr/bin/env python3
"""
External-plugin discovery test.

Proves a small analyzer that lives OUTSIDE src/datatrawl/ (here
tests/external_analyzer_fixture.py) is:
  * NOT visible as a built-in (a plain scan can't find it),
  * fully usable once loaded via `--plugin <path>` OR the DATATRAWL_PLUGINS env
    var, running through the real engine and honouring a `--set` parameter, and
  * strict about resume compatibility for both a `--set` option and capped
    smoke-test products, using AccumulatingAnalyzer's resume manifest.

Each case runs in a FRESH interpreter (subprocess `python -m datatrawl.cli`) so the
registry starts empty -- exactly a real user's situation.

Run:  PYTHONPATH=src python tests/test_external_plugin.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

from datatrawl.plugins.readers._baseband_format import NFFT, make_synth_file
from datatrawl import instruments as inst_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PLUGIN = os.path.join(ROOT, "tests", "external_analyzer_fixture.py")
F_TONE_BB = 12000.0
FREQ_ID = 844
ANALYZER = "fixture-mean-power"
NO_FRAME_CAP = -1
FLOAT_TOLERANCE = 1e-9
N_FILES = 3
FRAMES_PER_FILE = 6
N_FEEDS = 32


def _run(argv, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "datatrawl.cli", *argv],
                          capture_output=True, text=True, env=env)


def _make_lib(d, n=N_FILES):
    inst = inst_mod.load_instrument("chime")
    fc_mhz = inst.freq_of_freq_id(FREQ_ID)
    for k in range(n):
        make_synth_file(os.path.join(d, f"baseband_s{k}_{FREQ_ID}.h5"),
                        FRAMES_PER_FILE * NFFT, N_FEEDS, fc_mhz,
                        F_TONE_BB, seed=k + 1)


def _scan_argv(lib, root, tmp, extra=None):
    return ["scan", "--telescope", "chime", "--source", "local",
            "--reader", "chime-baseband", "--analyzer", ANALYZER,
            "--select", str(FREQ_ID), "--source-root", lib, "--root", root,
            "--tmp-dir", tmp, "--checkpoint-every", "1"] + (extra or [])


def _product_ok(root, scale_expected, cap_expected=NO_FRAME_CAP,
                files_expected=N_FILES) -> bool:
    p = os.path.join(root, "results", "chime", ANALYZER, f"{FREQ_ID}.npz")
    if not os.path.exists(p):
        print(f"  FAIL: product not written at {p}")
        return False
    ok = True
    with np.load(p, allow_pickle=False) as z:
        if str(z["analysis"]) != ANALYZER:
            print(f"  FAIL: analysis tag = {str(z['analysis'])!r}")
            ok = False
        if (int(z["frame_count"]) <= 0
                or not np.isfinite(float(z["mean_power"]))):
            print("  FAIL: fixture did not accumulate a finite mean power")
            ok = False
        if abs(float(z["fixture_scale"]) - scale_expected) > FLOAT_TOLERANCE:
            print(
                f"  FAIL: fixture_scale {float(z['fixture_scale'])} "
                f"!= {scale_expected} (--set did not reach ctx.options)"
            )
            ok = False
        if int(z["max_frames_per_file"]) != cap_expected:
            print(
                "  FAIL: max_frames_per_file "
                f"{int(z['max_frames_per_file'])} != {cap_expected}"
            )
            ok = False
        if int(z["files"].size) != files_expected:
            print(f"  FAIL: files {int(z['files'].size)} != {files_expected}")
            ok = False
    return ok


def _rejected(r, parameter: str) -> bool:
    text = r.stdout + r.stderr
    return r.returncode != 0 and parameter in text


def run_external_plugin() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_extplugin_")
    lib = os.path.join(work, "lib"); os.makedirs(lib)
    tmp = os.path.join(work, "tmp")
    _make_lib(lib)
    ok = True

    # 1. NOT a built-in: a plain scan cannot find it.
    r = _run(_scan_argv(lib, os.path.join(work, "none"), tmp))
    if r.returncode == 0:
        print("  FAIL: scan without --plugin should not find an external analyzer")
        ok = False
    elif ANALYZER not in (r.stderr + r.stdout):
        print(f"  FAIL: unexpected error (no mention of the analyzer):\n{r.stderr[-300:]}")
        ok = False
    else:
        print(f"  not-a-builtin: a plain scan correctly cannot find {ANALYZER!r}")

    # 2. Loaded via --plugin <path>, with a --set parameter.
    root2 = os.path.join(work, "viaflag")
    r = _run(_scan_argv(lib, root2, tmp,
                        extra=["--plugin", PLUGIN, "--set", "fixture_scale=1.5"]))
    if r.returncode != 0:
        print(f"  FAIL: --plugin scan returned {r.returncode}\n{r.stderr[-400:]}")
        ok = False
    elif not _product_ok(root2, 1.5):
        ok = False
    else:
        print("  via --plugin: external analyzer ran end-to-end, --set honoured")

    # 3. Loaded via the DATATRAWL_PLUGINS env var (no --plugin flag).
    root3 = os.path.join(work, "viaenv")
    r = _run(_scan_argv(lib, root3, tmp, extra=["--set", "fixture_scale=2"]),
             env_extra={"DATATRAWL_PLUGINS": PLUGIN})
    if r.returncode != 0:
        print(f"  FAIL: env-var scan returned {r.returncode}\n{r.stderr[-400:]}")
        ok = False
    elif not _product_ok(root3, 2.0):
        ok = False
    else:
        print("  via DATATRAWL_PLUGINS: same analyzer discovered through the env var")

    # 4. It also shows up in `list analyzers` when the plugin is loaded.
    r = _run(["list", "analyzers", "--plugin", PLUGIN])
    if ANALYZER not in r.stdout:
        print(f"  FAIL: {ANALYZER!r} missing from `list analyzers --plugin ...`")
        ok = False
    else:
        print(f"  discovery: {ANALYZER!r} appears in `list analyzers` once loaded")

    # 5. Analyzer-specific options are product invariants, even for a complete run.
    r = _run(_scan_argv(lib, root2, tmp,
                        extra=["--plugin", PLUGIN, "--set", "fixture_scale=3"]))
    if not _rejected(r, "fixture_scale"):
        print("  FAIL: changed fixture_scale did not reject resume")
        ok = False
    else:
        print("  resume validation: changed fixture_scale rejected")

    # 6. A capped smoke product can resume with the same cap, but not uncapped.
    root4 = os.path.join(work, "capped")
    capped = ["--plugin", PLUGIN, "--set", "fixture_scale=2.5",
              "--max-frames-per-file", "1"]
    r = _run(_scan_argv(lib, root4, tmp, extra=capped + ["--max-files", "1"]))
    if r.returncode != 0 or not _product_ok(root4, 2.5, 1, 1):
        print(f"  FAIL: capped one-file scan failed\n{r.stderr[-400:]}")
        ok = False

    r = _run(_scan_argv(lib, root4, tmp, extra=capped))
    if r.returncode != 0 or not _product_ok(root4, 2.5, 1, 3):
        print(f"  FAIL: matching capped resume failed\n{r.stderr[-400:]}")
        ok = False
    else:
        print("  resume validation: matching capped run completed the product")

    r = _run(_scan_argv(lib, root4, tmp,
                        extra=["--plugin", PLUGIN, "--set", "fixture_scale=2.5"]))
    if not _rejected(r, "max_frames_per_file"):
        print("  FAIL: uncapped run accepted a capped product")
        ok = False
    else:
        print("  resume validation: capped -> uncapped rejected")

    print("EXTERNAL PLUGIN SELF-TEST PASSED" if ok
          else "EXTERNAL PLUGIN SELF-TEST FAILED")
    result = 0 if ok else 1
    shutil.rmtree(work, ignore_errors=True)
    return result


def test_external_plugin_discovery():
    """pytest entry point: an out-of-repo analyzer loads via --plugin and env var."""
    assert run_external_plugin() == 0


if __name__ == "__main__":
    sys.exit(run_external_plugin())
