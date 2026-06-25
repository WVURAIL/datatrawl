#!/usr/bin/env python3
"""
External-plugin discovery test.

Proves an analyzer that lives OUTSIDE src/datatrawl/ (here the fixture
examples/external_analyzer.py) is:
  * NOT visible as a built-in (a plain scan can't find it), and
  * fully usable once loaded via `--plugin <path>` OR the DATATRAWL_PLUGINS env
    var, running through the real engine and honouring a `--set` parameter.

Each case runs in a FRESH interpreter (subprocess `python -m datatrawl.cli`) so the
registry starts empty -- exactly a real user's situation.

Run:  PYTHONPATH=src python tests/test_external_plugin.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import numpy as np

from datatrawl.plugins.readers._baseband_format import NFFT, FS, make_synth_file
from datatrawl import instruments as inst_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PLUGIN = os.path.join(ROOT, "examples", "external_analyzer.py")
F_TONE_BB = 12000.0
DF_HZ = FS / NFFT
FREQ_ID = 844


def _run(argv, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "datatrawl.cli", *argv],
                          capture_output=True, text=True, env=env)


def _make_lib(d, n=3):
    inst = inst_mod.load_instrument("chime")
    fc = inst.freq_of_freq_id(FREQ_ID) * 1e6
    for k in range(n):
        make_synth_file(os.path.join(d, f"baseband_s{k}_{FREQ_ID}.h5"),
                        6 * NFFT, 32, fc / 1e6, F_TONE_BB, seed=k + 1)
    return fc


def _scan_argv(lib, root, tmp, extra=None):
    return ["scan", "--telescope", "chime", "--source", "local",
            "--reader", "chime-baseband", "--analyzer", "freq_id-peak",
            "--select", str(FREQ_ID), "--source-root", lib, "--root", root,
            "--tmp-dir", tmp, "--checkpoint-every", "1"] + (extra or [])


def _product_ok(root, dc_mask_expected) -> bool:
    p = os.path.join(root, "results", "chime", "freq_id-peak", f"{FREQ_ID}.npz")
    if not os.path.exists(p):
        print(f"  FAIL: product not written at {p}")
        return False
    z = np.load(p, allow_pickle=False)
    ok = True
    if str(z["analysis"]) != "freq_id-peak":
        print(f"  FAIL: analysis tag = {str(z['analysis'])!r}"); ok = False
    if abs(float(z["peak_hz"]) - F_TONE_BB) > 2 * DF_HZ:
        print(f"  FAIL: peak {float(z['peak_hz']):+.1f} off tone {F_TONE_BB:+.0f}")
        ok = False
    if abs(float(z["dc_mask_hz"]) - dc_mask_expected) > 1e-9:
        print(f"  FAIL: dc_mask_hz {float(z['dc_mask_hz'])} != {dc_mask_expected} "
              "(--set did not reach ctx.options)"); ok = False
    if int(z["freq_id"]) != FREQ_ID:
        print(f"  FAIL: freq_id {int(z['freq_id'])}"); ok = False
    return ok


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
    elif "freq_id-peak" not in (r.stderr + r.stdout):
        print(f"  FAIL: unexpected error (no mention of the analyzer):\n{r.stderr[-300:]}")
        ok = False
    else:
        print("  not-a-builtin: a plain scan correctly cannot find 'freq_id-peak'")

    # 2. Loaded via --plugin <path>, with a --set parameter.
    root2 = os.path.join(work, "viaflag")
    r = _run(_scan_argv(lib, root2, tmp,
                        extra=["--plugin", PLUGIN, "--set", "dc_mask_hz=50"]))
    if r.returncode != 0:
        print(f"  FAIL: --plugin scan returned {r.returncode}\n{r.stderr[-400:]}")
        ok = False
    elif not _product_ok(root2, 50.0):
        ok = False
    else:
        print("  via --plugin: external analyzer ran end-to-end, --set honoured")

    # 3. Loaded via the DATATRAWL_PLUGINS env var (no --plugin flag).
    root3 = os.path.join(work, "viaenv")
    r = _run(_scan_argv(lib, root3, tmp, extra=["--set", "dc_mask_hz=0"]),
             env_extra={"DATATRAWL_PLUGINS": PLUGIN})
    if r.returncode != 0:
        print(f"  FAIL: env-var scan returned {r.returncode}\n{r.stderr[-400:]}")
        ok = False
    elif not _product_ok(root3, 0.0):
        ok = False
    else:
        print("  via DATATRAWL_PLUGINS: same analyzer discovered through the env var")

    # 4. It also shows up in `list analyzers` when the plugin is loaded.
    r = _run(["list", "analyzers", "--plugin", PLUGIN])
    if "freq_id-peak" not in r.stdout:
        print("  FAIL: 'freq_id-peak' missing from `list analyzers --plugin ...`")
        ok = False
    else:
        print("  discovery: 'freq_id-peak' appears in `list analyzers` once loaded")

    print("EXTERNAL PLUGIN SELF-TEST PASSED" if ok
          else "EXTERNAL PLUGIN SELF-TEST FAILED")
    return 0 if ok else 1


def test_external_plugin_discovery():
    """pytest entry point: an out-of-repo analyzer loads via --plugin and env var."""
    assert run_external_plugin() == 0


if __name__ == "__main__":
    sys.exit(run_external_plugin())
