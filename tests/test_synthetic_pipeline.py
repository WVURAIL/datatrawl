#!/usr/bin/env python3
"""
End-to-end self-test for the GENERIC engine on a synthetic library.

This is the proof that `datatrawl.pipeline.run` -- the telescope/reader/
analyzer-agnostic streaming engine -- works with the real plugins and NO access
to CANFAR/CADC. It wires the production path:

    LocalDirectorySource  ->  ChimeBasebandReader  ->  PowerSpectrumAnalyzer

over synthetic CHIME-baseband HDF5 files with a CW tone injected at a known
baseband frequency, and checks:

  1. recovery     -- the averaged PSD peaks at the injected tone;
  2. schema       -- the product .npz carries the spectrum key set, incl. the
                     `analysis` signature and the sky frequency axis;
  3. provenance   -- every input file is recorded with its Unit.key (so resume
                     can skip it);
  4. checkpoint   -- a mid-run atomic checkpoint leaves a loadable product;
  5. resume       -- re-running is a no-op (all units already in the product);
  6. partial      -- a product built from a subset is completed, not restarted;
  7. fan-out      -- a multi-freq_id --select produces one product per freq_id;
  8. max-frames   -- --max-frames-per-file caps the per-file work (the quick
                     smoke-test path).

Run:  PYTHONPATH=src python tests/test_synthetic_pipeline.py
Exit: 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np

from datatrawl.plugins.readers._baseband_format import FS, NFFT, make_synth_file
from datatrawl import instruments as inst_mod
from datatrawl import pipeline
from datatrawl.interfaces import RunContext
from datatrawl.plugins.sources.local import LocalDirectorySource
from datatrawl.plugins.readers.chime_baseband import ChimeBasebandReader
from datatrawl.plugins.analyzers.spectrum import PowerSpectrumAnalyzer

# The spectrum product schema. Keep in sync with PowerSpectrumAnalyzer.save();
# this set IS the contract under test.
REQUIRED_KEYS = {
    "analysis", "psd", "psd_sum", "count", "freqs_hz", "freqs_sky_hz",
    "f_center_hz", "freq_id", "nfft", "fs_hz", "nyquist_zone", "files", "unit_keys",
    "created",
}

F_TONE_BB = 12000.0          # injected baseband tone (Hz)
DF_HZ = FS / NFFT            # FFT bin width (~23.84 Hz)


def make_library(d: str, n_files: int, freq_id: int = 844):
    """Write n_files synthetic CHIME-baseband files for ONE freq_id.

    The tone is injected at F_TONE_BB (baseband), so the averaged PSD must peak
    there. Returns (freq_id, f_center_hz).
    """
    inst = inst_mod.load_instrument("chime")
    f_center = inst.freq_of_freq_id(freq_id) * 1e6      # Hz
    for k in range(n_files):
        p = os.path.join(d, f"baseband_synth{k}_{freq_id}.h5")
        make_synth_file(p, 6 * NFFT, 32, f_center / 1e6, F_TONE_BB, seed=k + 1)
    return freq_id, f_center


def make_ctx(root: str, freq_id: int) -> RunContext:
    inst = inst_mod.load_instrument("chime")
    return RunContext(instrument=inst, selection=[freq_id],
                      options={"source_root": root, "source_glob": "*.h5"})


def scan(root: str, out_path: str, tmp_dir: str, freq_id: int,
         checkpoint_every: int = 50, max_files=None, max_frames_per_file=None):
    """Run the generic engine over `root` exactly as `datatrawl scan` does."""
    src = LocalDirectorySource()
    rdr = ChimeBasebandReader()
    red = PowerSpectrumAnalyzer()
    ctx = make_ctx(root, freq_id)
    units = list(src.enumerate(ctx))
    return pipeline.run(
        source=src, reader=rdr, analyzer=red, units=units,
        out_path=out_path, tmp_dir=tmp_dir, ctx=ctx,
        checkpoint_every=checkpoint_every, download_workers=2,
        max_files=max_files, max_frames_per_file=max_frames_per_file,
        verbose=False,
    )


def _fail(msg: str) -> None:
    print(f"  FAIL: {msg}")


def main() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_selftest_")
    lib = os.path.join(work, "lib"); os.makedirs(lib)
    tmp = os.path.join(work, "scratch")
    out = os.path.join(work, "out", "chan_test.npz")
    n_files = 3
    freq_id, f_center = make_library(lib, n_files)
    print(f"synthetic library: {n_files} files, freq_id {freq_id}, "
          f"f_center {f_center/1e6:.6f} MHz, tone {F_TONE_BB:+.0f} Hz baseband")

    ok = True

    # -- full run, with a checkpoint forced mid-stream (every 2 of 3 files) -----
    res = scan(lib, out, tmp, freq_id, checkpoint_every=2)
    if res.n_new != n_files or res.n_failed != 0:
        _fail(f"expected {n_files} new / 0 failed, got {res.n_new}/{res.n_failed}")
        ok = False

    # scratch must be empty: the engine deletes each staged file after reducing.
    leftovers = os.listdir(tmp) if os.path.isdir(tmp) else []
    if leftovers:
        _fail(f"scratch not cleaned, leftovers: {leftovers}")
        ok = False
    if len(os.listdir(lib)) != n_files:
        _fail("local source deleted or added to the original library")
        ok = False

    z = np.load(out, allow_pickle=False)
    keys = set(z.files)

    # (2) schema
    missing = REQUIRED_KEYS - keys
    if missing:
        _fail(f"product missing keys: {sorted(missing)}")
        ok = False
    if "analysis" not in keys or str(z["analysis"]) != "spectrum":
        _fail("product missing analysis='spectrum' signature")
        ok = False

    # (1) recovery: averaged PSD peaks at the injected tone
    psd, freqs, sky = z["psd"], z["freqs_hz"], z["freqs_sky_hz"]
    k = int(np.argmax(psd))
    if abs(freqs[k] - F_TONE_BB) > 2 * DF_HZ:
        _fail(f"PSD peak {freqs[k]:+.1f} Hz off injected {F_TONE_BB:+.1f} "
              f"(df {DF_HZ:.1f})")
        ok = False
    sign = inst_mod.nyquist_sign(int(z["nyquist_zone"]))
    if not np.allclose(sky, f_center + sign * freqs, atol=1.0):
        _fail("freqs_sky_hz != f_center + sign*freqs_hz")
        ok = False

    # (3) provenance
    count = int(z["count"])
    if count <= 0:
        _fail(f"count {count} should be > 0"); ok = False
    if z["files"].size != n_files:
        _fail(f"files recorded {z['files'].size} != {n_files}"); ok = False
    if z["unit_keys"].size != n_files:
        _fail(f"unit_keys {z['unit_keys'].size} != {n_files} (resume would break)")
        ok = False
    if int(z["nfft"]) != NFFT or float(z["fs_hz"]) != FS:
        _fail("product geometry stamp does not match the engine constants")
        ok = False
    if int(z["freq_id"]) != int(freq_id):
        _fail(f"product freq_id {int(z['freq_id'])} != {int(freq_id)}"); ok = False

    peak_sky = f_center + sign * freqs[k]
    print(f"  recovery: PSD peak {freqs[k]:+.1f} Hz baseband "
          f"({peak_sky/1e6:.4f} MHz sky), {count} frames over "
          f"{z['files'].size} files")

    # (5) resume = no-op
    res2 = scan(lib, out, tmp, freq_id, checkpoint_every=2)
    if res2.n_new != 0:
        _fail(f"resume reprocessed {res2.n_new} unit(s); expected 0"); ok = False
    z2 = np.load(out, allow_pickle=False)
    if int(z2["count"]) != count:
        _fail(f"resume changed count {int(z2['count'])} != {count}"); ok = False
    print(f"  resume: no-op confirmed (count stable at {int(z2['count'])})")

    # (6) partial product completed, not restarted
    out_p = os.path.join(work, "out", "chan_partial.npz")
    scan(lib, out_p, tmp, freq_id, max_files=1)              # only 1 file
    z_a = np.load(out_p, allow_pickle=False)
    if int(z_a["files"].size) != 1:
        _fail(f"partial product holds {int(z_a['files'].size)} files, expected 1")
        ok = False
    count_a = int(z_a["count"])
    r_b = scan(lib, out_p, tmp, freq_id)                     # now all files
    z_b = np.load(out_p, allow_pickle=False)
    if int(z_b["files"].size) != n_files:
        _fail(f"completed product holds {int(z_b['files'].size)}, expected {n_files}")
        ok = False
    if r_b.n_new != n_files - 1:
        _fail(f"completion processed {r_b.n_new}, expected {n_files - 1}"); ok = False
    if int(z_b["count"]) <= count_a:
        _fail(f"frame count not monotonic: {count_a} -> {int(z_b['count'])}")
        ok = False
    print(f"  partial->complete: {int(z_a['files'].size)} file -> "
          f"{int(z_b['files'].size)} files, frames {count_a} -> {int(z_b['count'])}")

    print("GENERIC PIPELINE SELF-TEST PASSED" if ok
          else "GENERIC PIPELINE SELF-TEST FAILED")
    return 0 if ok else 1


def test_generic_pipeline_synthetic():
    """pytest entry point: the end-to-end run must pass all checks."""
    assert main() == 0


# ---------------------------------------------------------------------------
# Per-freq_id fan-out: a multi-freq_id --select must produce one independent,
# resumable product per freq_id.
# ---------------------------------------------------------------------------
def _make_two_freq_id_library(d: str, freq_ids=(614, 706)):
    """Two freq_ids, 2 files each, freq_id in the filename, tone injected."""
    inst = inst_mod.load_instrument("chime")
    for ch in freq_ids:
        f_center = inst.freq_of_freq_id(ch) * 1e6
        for k in range(2):
            p = os.path.join(d, f"baseband_ev{ch}{k}_{ch}.h5")
            make_synth_file(p, 6 * NFFT, 32, f_center / 1e6, F_TONE_BB, seed=ch + k)
    return sorted(freq_ids)


def _cli_scan(lib, root, sel, work, extra=None):
    import datatrawl.cli as cli
    argv = ["scan", "--telescope", "chime", "--source", "local",
            "--reader", "chime-baseband", "--analyzer", "spectrum",
            "--source-root", lib, "--select", sel, "--root", root,
            "--tmp-dir", os.path.join(work, "tmp"), "--checkpoint-every", "1"]
    return cli.main(argv + (extra or []))


def _run_fanout() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_fanout_")
    lib = os.path.join(work, "lib"); os.makedirs(lib)
    freq_ids = _make_two_freq_id_library(lib)
    sel = ",".join(str(c) for c in freq_ids)
    root = os.path.join(work, "run")
    ok = True

    if _cli_scan(lib, root, sel, work) != 0:
        _fail("multi-freq_id scan returned nonzero"); ok = False

    counts = {}
    for ch in freq_ids:
        p = os.path.join(root, "results", "chime", "spectrum", f"{ch}.npz")
        if not os.path.exists(p):
            _fail(f"missing per-freq_id product {ch}.npz"); ok = False; continue
        z = np.load(p, allow_pickle=False)
        counts[ch] = int(z["count"])
        if int(z["freq_id"]) != ch:
            _fail(f"product {ch}.npz has freq_id {int(z['freq_id'])}"); ok = False
        if int(z["files"].size) != 2:
            _fail(f"product {ch}.npz holds {int(z['files'].size)} files, expected 2")
            ok = False
        psd, freqs = z["psd"], z["freqs_hz"]
        if abs(freqs[int(np.argmax(psd))] - F_TONE_BB) > 2 * DF_HZ:
            _fail(f"freq_id {ch}: PSD peak off injected tone"); ok = False

    if len(set(freq_ids)) != 2:
        _fail("test setup produced only one freq_id"); ok = False

    # resume: re-running is a no-op, counts unchanged
    if _cli_scan(lib, root, sel, work) != 0:
        _fail("resume scan returned nonzero"); ok = False
    for ch in freq_ids:
        z = np.load(os.path.join(root, "results", "chime", "spectrum",
                    f"{ch}.npz"), allow_pickle=False)
        if int(z["count"]) != counts.get(ch):
            _fail(f"resume changed freq_id {ch} count"); ok = False

    print(f"  fan-out: select '{sel}' -> {len(freq_ids)} independent products "
          f"{freq_ids}, each resumed clean")
    print("PER-FREQ_ID FAN-OUT SELF-TEST PASSED" if ok
          else "PER-FREQ_ID FAN-OUT SELF-TEST FAILED")
    return 0 if ok else 1


def test_per_freq_id_fanout():
    """pytest entry point: a multi-freq_id select fans out to per-freq_id products."""
    assert _run_fanout() == 0


# ---------------------------------------------------------------------------
# --max-frames-per-file caps the per-file work (the quick-test path).
# ---------------------------------------------------------------------------
def _run_max_frames() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_maxframes_")
    lib = os.path.join(work, "lib"); os.makedirs(lib)
    n_files = 3
    freq_id, _ = make_library(lib, n_files)
    root = os.path.join(work, "run")
    cap = 3                                  # each file is 6*NFFT = 6 frames
    ok = True
    if _cli_scan(lib, root, str(freq_id), work,
                 extra=["--max-frames-per-file", str(cap)]) != 0:
        _fail("capped scan returned nonzero"); ok = False
    z = np.load(os.path.join(root, "results", "chime", "spectrum",
                f"{freq_id}.npz"), allow_pickle=False)
    if int(z["count"]) != n_files * cap:
        _fail(f"--max-frames-per-file {cap}: count {int(z['count'])} "
              f"!= {n_files * cap}"); ok = False
    print(f"  max-frames-per-file={cap}: {n_files} files -> "
          f"{int(z['count'])} frames (= {n_files}x{cap})")
    print("MAX-FRAMES-PER-FILE SELF-TEST PASSED" if ok
          else "MAX-FRAMES-PER-FILE SELF-TEST FAILED")
    return 0 if ok else 1


def test_max_frames_per_file():
    """pytest entry point: --max-frames-per-file caps the per-file frame count."""
    assert _run_max_frames() == 0


# ---------------------------------------------------------------------------
# --nfft overrides the frame/FFT length for the run (default: the instrument YAML).
# ---------------------------------------------------------------------------
def _run_nfft_override() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_nfft_")
    lib = os.path.join(work, "lib"); os.makedirs(lib)
    n_files = 2
    freq_id, _ = make_library(lib, n_files)
    root = os.path.join(work, "run")
    new_nfft = NFFT // 2                          # 8192
    ok = True
    if _cli_scan(lib, root, str(freq_id), work,
                 extra=["--nfft", str(new_nfft)]) != 0:
        _fail("nfft-override scan returned nonzero"); ok = False
    z = np.load(os.path.join(root, "results", "chime", "spectrum",
                f"{freq_id}.npz"), allow_pickle=False)
    if int(z["nfft"]) != new_nfft:
        _fail(f"product nfft {int(z['nfft'])} != {new_nfft}"); ok = False
    if z["freqs_hz"].size != new_nfft:
        _fail(f"freqs_hz size {z['freqs_hz'].size} != {new_nfft}"); ok = False
    expect = n_files * (6 * NFFT // new_nfft)     # each file is 6*NFFT samples
    if int(z["count"]) != expect:
        _fail(f"--nfft {new_nfft}: count {int(z['count'])} != {expect}"); ok = False
    print(f"  nfft override: --nfft {new_nfft} -> product nfft {int(z['nfft'])}, "
          f"{int(z['count'])} frames (= {n_files}x{6 * NFFT // new_nfft})")
    print("NFFT-OVERRIDE SELF-TEST PASSED" if ok else "NFFT-OVERRIDE SELF-TEST FAILED")
    return 0 if ok else 1


def test_nfft_override():
    """pytest entry point: --nfft overrides the frame/FFT length for the run."""
    assert _run_nfft_override() == 0


if __name__ == "__main__":
    rc = main()
    rc = _run_fanout() or rc
    rc = _run_max_frames() or rc
    rc = _run_nfft_override() or rc
    sys.exit(rc)
