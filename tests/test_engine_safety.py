#!/usr/bin/env python3
"""
Regression tests for the engine/resume hardening (the external review findings).

Each test pins a behavior that previously could silently violate one of the
headline guarantees -- bounded scratch, product compatibility, "resume means
complete" -- or a source-selection / readiness bug. They run fully offline.

Coverage map (review finding -> tests):
  #1 bounded scratch          -> test_staged_file_bound_* , scratch-empty asserts
  #2 unique scratch names      -> test_stage_name_unique , test_duplicate_basenames_*
  #3 resume compatibility      -> test_resume_rejects_{freq_id,nfft,nyquist_zone} , _accepts_match
  #4 capped != complete        -> test_resume_rejects_cap_change
  #5 reduction order           -> test_default_delivers_source_order
  #7 local freq_id selection   -> test_local_source_44_not_844 , _duplicate_basenames_*
  #8 doctor readiness          -> test_geometry_only_telescope_not_archive_ready

Run:  PYTHONPATH=src python -m pytest tests/test_engine_safety.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

import numpy as np
import pytest

from datatrawl import instruments as inst_mod
from datatrawl import pipeline
from datatrawl.pipeline import _stage_name
from datatrawl.interfaces import (DataSource, Analyzer, RunContext, Unit,
                                  PluginInfo, READY)
from datatrawl.plugins.readers._baseband_format import NFFT, make_synth_file
from datatrawl.plugins.readers.chime_baseband import ChimeBasebandReader
from datatrawl.plugins.analyzers.spectrum import PowerSpectrumAnalyzer
from datatrawl.plugins.sources.local import LocalDirectorySource

F_TONE_BB = 12000.0
_SENTINEL = "__none__"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tmp(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=f"dt_{prefix}_")


def _chime_ctx(root, freq_id, nfft=None, nyquist_zone=None, cap=_SENTINEL) -> RunContext:
    """A scan-like context for the chime instrument, with optional overrides."""
    inst = inst_mod.load_instrument("chime")
    if nfft is not None:
        inst.nfft = nfft
    if nyquist_zone is not None:
        inst.nyquist_zone = nyquist_zone
    opts = {"source_root": root, "source_glob": "*.h5"}
    if cap is not _SENTINEL:
        opts["max_frames_per_file"] = cap          # mimic pipeline.run's injection
    return RunContext(instrument=inst, selection=[freq_id], options=opts)


def _build_product(work, freq_id=844, n_files=1, cap=None):
    """Build a real spectrum product for `freq_id` via the engine; return its path."""
    lib = os.path.join(work, "lib"); os.makedirs(lib, exist_ok=True)
    tmp = os.path.join(work, "tmp")
    out = os.path.join(work, f"prod_{freq_id}.npz")
    inst = inst_mod.load_instrument("chime")
    fcen = inst.freq_of_freq_id(freq_id) * 1e6
    for k in range(n_files):
        make_synth_file(os.path.join(lib, f"baseband_b{k}_{freq_id}.h5"),
                        6 * NFFT, 16, fcen / 1e6, F_TONE_BB, seed=k + 1)
    src, rdr, red = LocalDirectorySource(), ChimeBasebandReader(), PowerSpectrumAnalyzer()
    ctx = _chime_ctx(lib, freq_id, cap=cap)
    units = list(src.enumerate(ctx))
    pipeline.run(source=src, reader=rdr, analyzer=red, units=units, out_path=out,
                 tmp_dir=tmp, ctx=ctx, max_frames_per_file=cap, verbose=False)
    return out


class _FakeSynthSource(DataSource):
    """A source that synthesizes a baseband file per unit on fetch().

    Lets a test control unit identity (key/name) and fetch timing independently
    of the local filesystem, and records the peak number of files co-resident on
    scratch so the storage bound can be asserted.
    """
    info = PluginInfo(name="fake", kind="source",
                      summary="synthetic in-test source", status=READY,
                      instruments=("*",))

    def __init__(self, fcen_hz, n_frames=4, delay_keys=()):
        self._fcen = fcen_hz
        self._nframes = n_frames
        self._delay = set(delay_keys)
        self._lock = threading.Lock()
        self.max_on_disk = 0
        self.fetch_order = []

    def enumerate(self, ctx):                       # not used by pipeline.run
        return []

    def fetch(self, unit, dest):
        if unit.key in self._delay:
            time.sleep(0.25)                        # make this unit "slow"
        make_synth_file(dest, self._nframes * NFFT, 16, self._fcen / 1e6,
                        F_TONE_BB, seed=(abs(hash(unit.key)) % 997) + 1)
        with self._lock:
            self.fetch_order.append(unit.key)
            n = len(os.listdir(os.path.dirname(dest)))
            self.max_on_disk = max(self.max_on_disk, n)
        return True, ""


class _RecordingAnalyzer(Analyzer):
    """Minimal analyzer that records the order files are delivered in."""
    info = PluginInfo(name="rec", kind="analyzer", summary="records delivery order",
                      status=READY, instruments=("*",))

    def __init__(self):
        self.order = []

    def resume(self, path, ctx):
        return False

    def processed_keys(self):
        return set()

    def begin(self, ctx, first_meta):
        pass

    def consume_file(self, arrays, meta):
        list(arrays)                                # drain so the engine deletes it
        self.order.append(meta["unit_key"])
        return 0

    def save(self, path):
        np.savez(path, order=np.array(self.order))

    def summary(self):
        return {"count": len(self.order)}


# ---------------------------------------------------------------------------
# #3 / #4  resume compatibility
# ---------------------------------------------------------------------------
def test_resume_accepts_matching_run():
    """A resume with identical invariants must be allowed (no false positives)."""
    work = _tmp("resume_ok")
    out = _build_product(work, freq_id=844)
    assert PowerSpectrumAnalyzer().resume(out, _chime_ctx(work, 844)) is True


def test_resume_rejects_different_freq_id():
    work = _tmp("resume_ch")
    out = _build_product(work, freq_id=844)           # product is freq_id 844
    with pytest.raises(SystemExit):
        PowerSpectrumAnalyzer().resume(out, _chime_ctx(work, 706))   # run is 706


def test_resume_rejects_different_nfft():
    work = _tmp("resume_nfft")
    out = _build_product(work, freq_id=844)           # nfft 16384
    with pytest.raises(SystemExit):
        PowerSpectrumAnalyzer().resume(out, _chime_ctx(work, 844, nfft=NFFT // 2))


def test_resume_rejects_different_nyquist_zone():
    work = _tmp("resume_zone")
    out = _build_product(work, freq_id=844)           # chime is nyquist_zone 2
    with pytest.raises(SystemExit):
        PowerSpectrumAnalyzer().resume(out, _chime_ctx(work, 844, nyquist_zone=1))


def test_resume_rejects_cap_change():
    """A capped smoke-test product must not be 'completed' by a full run."""
    work = _tmp("resume_cap")
    out = _build_product(work, freq_id=844, n_files=1, cap=2)   # capped, stamped
    with pytest.raises(SystemExit):
        PowerSpectrumAnalyzer().resume(out, _chime_ctx(work, 844))  # uncapped run


def test_capped_product_stamps_the_cap():
    work = _tmp("cap_stamp")
    out = _build_product(work, freq_id=844, n_files=1, cap=2)
    z = np.load(out, allow_pickle=False)
    assert "max_frames_per_file" in z.files
    assert int(z["max_frames_per_file"]) == 2
    assert int(z["count"]) == 2                        # 1 file x cap 2


# ---------------------------------------------------------------------------
# #2  unique scratch names
# ---------------------------------------------------------------------------
def test_stage_name_unique_for_same_basename_different_key():
    u1 = Unit(key="src://A/baseband_dup_844.h5", name="baseband_dup_844.h5")
    u2 = Unit(key="src://B/baseband_dup_844.h5", name="baseband_dup_844.h5")
    assert _stage_name(u1) != _stage_name(u2)
    # the human-readable basename is preserved as a suffix for provenance
    assert _stage_name(u1).endswith("baseband_dup_844.h5")


def test_duplicate_basenames_distinct_keys_both_processed():
    """Two units sharing a basename must not collide on scratch (provenance + data)."""
    work = _tmp("dup")
    inst = inst_mod.load_instrument("chime")
    fcen = inst.freq_of_freq_id(844) * 1e6
    u1 = Unit(key="src://A/baseband_dup_844.h5", name="baseband_dup_844.h5",
              meta={"f_center_hz": fcen})
    u2 = Unit(key="src://B/baseband_dup_844.h5", name="baseband_dup_844.h5",
              meta={"f_center_hz": fcen})
    src = _FakeSynthSource(fcen, n_frames=4)
    ctx = RunContext(instrument=inst, selection=[844], options={})
    out = os.path.join(work, "dup.npz"); tmp = os.path.join(work, "tmp")
    res = pipeline.run(source=src, reader=ChimeBasebandReader(),
                       analyzer=PowerSpectrumAnalyzer(), units=[u1, u2], out_path=out,
                       tmp_dir=tmp, ctx=ctx, download_workers=2, verbose=False)
    z = np.load(out, allow_pickle=False)
    assert res.n_new == 2
    assert set(map(str, z["unit_keys"])) == {u1.key, u2.key}   # both, distinct
    assert int(z["count"]) == 2 * 4                            # both contributed
    assert os.listdir(tmp) == []                               # scratch clean


# ---------------------------------------------------------------------------
# #1  bounded scratch
# ---------------------------------------------------------------------------
def _bound_run(work, n_units, workers, staged):
    inst = inst_mod.load_instrument("chime")
    fcen = inst.freq_of_freq_id(844) * 1e6
    units = [Unit(key=f"src://{i}/f_{i}_844.h5", name=f"f_{i}_844.h5",
                  meta={"f_center_hz": fcen}) for i in range(n_units)]
    src = _FakeSynthSource(fcen, n_frames=2)
    ctx = RunContext(instrument=inst, selection=[844], options={})
    out = os.path.join(work, "b.npz"); tmp = os.path.join(work, "tmp")
    pipeline.run(source=src, reader=ChimeBasebandReader(),
                 analyzer=PowerSpectrumAnalyzer(), units=units, out_path=out,
                 tmp_dir=tmp, ctx=ctx, download_workers=workers,
                 max_staged_files=staged, verbose=False)
    return src.max_on_disk, tmp


def test_staged_file_bound_one_by_default():
    """Even with 4 download workers, the default holds exactly one file on scratch."""
    work = _tmp("bound1")
    peak, tmp = _bound_run(work, n_units=6, workers=4, staged=1)
    assert peak <= 1
    assert os.listdir(tmp) == []


def test_staged_file_bound_respects_max_staged_files():
    """Raising the bound to N never exceeds N files on scratch."""
    work = _tmp("bound3")
    peak, tmp = _bound_run(work, n_units=8, workers=4, staged=3)
    assert peak <= 3
    assert os.listdir(tmp) == []


# ---------------------------------------------------------------------------
# #5  reduction order under the default
# ---------------------------------------------------------------------------
def test_default_delivers_source_order_even_if_later_file_is_faster():
    work = _tmp("order")
    inst = inst_mod.load_instrument("chime")
    fcen = inst.freq_of_freq_id(844) * 1e6
    u1 = Unit(key="k1", name="f1_844.h5", meta={"f_center_hz": fcen})
    u2 = Unit(key="k2", name="f2_844.h5", meta={"f_center_hz": fcen})
    # u1 is slow, u2 is fast -- under the default (1 worker) u1 still arrives first.
    src = _FakeSynthSource(fcen, n_frames=2, delay_keys={"k1"})
    red = _RecordingAnalyzer()
    ctx = RunContext(instrument=inst, selection=[844], options={})
    out = os.path.join(work, "o.npz"); tmp = os.path.join(work, "tmp")
    pipeline.run(source=src, reader=ChimeBasebandReader(), analyzer=red,
                 units=[u1, u2], out_path=out, tmp_dir=tmp, ctx=ctx, verbose=False)
    assert red.order == ["k1", "k2"]


# ---------------------------------------------------------------------------
# #7  local source freq_id selection
# ---------------------------------------------------------------------------
def _touch(path):
    with open(path, "wb") as fh:
        fh.write(b"\x00")


def test_local_source_44_not_844():
    work = _tmp("sel")
    _touch(os.path.join(work, "baseband_s0_44.h5"))
    _touch(os.path.join(work, "baseband_s0_844.h5"))
    inst = inst_mod.load_instrument("chime")
    ctx = RunContext(instrument=inst, selection=[44],
                     options={"source_root": work, "source_glob": "*.h5"})
    names = sorted(u.name for u in LocalDirectorySource().enumerate(ctx))
    assert names == ["baseband_s0_44.h5"]              # 844 must NOT match 44


def test_local_source_duplicate_basenames_distinct_keys():
    work = _tmp("dupsel")
    a = os.path.join(work, "a"); b = os.path.join(work, "b")
    os.makedirs(a); os.makedirs(b)
    _touch(os.path.join(a, "baseband_x_844.h5"))
    _touch(os.path.join(b, "baseband_x_844.h5"))
    inst = inst_mod.load_instrument("chime")
    ctx = RunContext(instrument=inst, selection=[844],
                     options={"source_root": work, "source_glob": "*.h5"})
    units = list(LocalDirectorySource().enumerate(ctx))
    assert len(units) == 2                             # both subdirs enumerated
    assert len({u.key for u in units}) == 2            # distinct keys
    assert len({_stage_name(u) for u in units}) == 2   # -> distinct scratch names


# ---------------------------------------------------------------------------
# #8  doctor readiness
# ---------------------------------------------------------------------------
def test_geometry_only_telescope_not_archive_ready():
    """The readiness rule `doctor` enforces: a telescope with geometry + `nyquist_zone`
    but no declared baseband `scopes` is usable with a LOCAL source and counts as
    'geometry-only' -- not an out-of-the-box archive combo (it still works against an
    archive source if you pass --scope).

    Hermetic by construction -- it writes a throwaway geometry-only instrument and
    checks the readiness logic directly, so it depends neither on which shipped
    telescopes happen to declare scopes (all current ones do) nor on local CADC
    packages / proxy-cert state."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "geomonly.yaml"), "w") as f:
            f.write(
                "name: geomonly\n"
                "band: {f0_mhz: 800.0, bandwidth_mhz: 400.0, n_channels: 1024}\n"
                "nyquist_zone: 2\n"   # geometry + Nyquist zone set, but no scopes declared
            )
        rd = inst_mod.instrument_readiness("geomonly", directory=d)
        assert rd.nyquist_zone_set is True
        assert rd.scopes_set is False
        assert rd.ready is False
        assert rd.status == "geometry-only"
        assert rd.usable_for(needs_archive_config=True) is False    # archive combo: not auto-ready
        assert rd.usable_for(needs_archive_config=False) is True     # local: fine


def test_scan_rejects_nonpositive_int_options():
    """Numeric scan options must be positive. `--checkpoint-every 0` would hit a
    `got % checkpoint_every` zero-division at runtime, and zero/negative worker or
    file caps are meaningless -- argparse rejects them up front instead."""
    import datatrawl.cli as cli
    parser = cli.build_parser()
    for opt in ("--checkpoint-every", "--download-workers", "--max-staged-files",
                "--max-files", "--max-frames-per-file"):
        for bad in ("0", "-1"):
            with pytest.raises(SystemExit):
                parser.parse_args(["scan", "--analyzer", "spectrum", opt, bad])
    # a positive value still parses
    args = parser.parse_args(["scan", "--analyzer", "spectrum",
                              "--checkpoint-every", "10"])
    assert args.checkpoint_every == 10


if __name__ == "__main__":
    rc = pytest.main([__file__, "-q"])
    sys.exit(rc)
