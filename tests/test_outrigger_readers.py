#!/usr/bin/env python3
"""
Offline tests for the outrigger readers (gains and N-squared).

Both readers register at import time like every built-in plugin. The
N-squared reader's `hdf5plugin` dependency is optional (`[outriggers]`
extra): the module imports without it and `preflight` reports the gap, so
`doctor` can flag it before a run. Synthetic HDF5 files (uncompressed, so
no filters are needed) exercise `probe` end to end.

Run:  PYTHONPATH=src python tests/test_outrigger_readers.py
"""
from __future__ import annotations

import os
import tempfile
from datetime import timezone

import numpy as np
import h5py
import pytest

from datatrawl import registry
from datatrawl.interfaces import RunContext, UnreadableUnitError
from datatrawl.instruments import load_instrument
from datatrawl.plugins.readers.chime_baseband import ChimeBasebandReader
from datatrawl.plugins.readers.outrigger_gains_reader import OutriggerGainsReader
from datatrawl.plugins.readers import outrigger_n2_reader
from datatrawl.plugins.readers.outrigger_n2_reader import OutriggerN2Reader


def test_both_readers_registered():
    """`datatrawl list readers` must show the outrigger readers."""
    registry.load_plugins()
    names = set(registry.available("reader"))
    assert {"chime-baseband", "outrigger-gains", "outrigger-n2"} <= names


def test_gains_filename_parsing():
    meta = OutriggerGainsReader.parse_filename(
        "gain_20250114T093512.123456Z_cyga_noise_weighted.h5")
    assert meta["calibrator"] == "cyga"
    assert meta["noise_weighted"] is True
    assert meta["timestamp"].tzinfo == timezone.utc
    assert meta["timestamp"].year == 2025

    plain = OutriggerGainsReader.parse_filename("gain_20250114T093512.0Z_casa.h5")
    assert plain["noise_weighted"] is False

    try:
        OutriggerGainsReader.parse_filename("not_a_gain_file.h5")
        raise AssertionError("expected ValueError for a non-gain filename")
    except ValueError:
        pass


def test_n2_folder_parsing():
    parsed = OutriggerN2Reader.parse_folder_name("20250114T093512Z_gbostack_corr")
    assert parsed["site"] == "gbo"
    assert parsed["variant"] == "stack"
    plain = OutriggerN2Reader.parse_folder_name("20250114T093512Z_kko_corr")
    assert plain["variant"] == "plain"

    try:
        OutriggerN2Reader.parse_folder_name("random_dir")
        raise AssertionError("expected ValueError for a non-N2 folder name")
    except ValueError:
        pass


def test_gains_probe_on_synthetic_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gain_20250114T093512.5Z_cyga.h5")
        with h5py.File(path, "w") as f:
            f.create_dataset("gain", data=np.zeros((16, 4), dtype=np.complex64))
        meta = OutriggerGainsReader().probe(path)
        assert meta["shape"] == (16, 4)
        assert meta["calibrator"] == "cyga"


def test_n2_probe_on_synthetic_file():
    with tempfile.TemporaryDirectory() as d:
        # unrecognized dir name -> the site/variant product check is skipped,
        # but the fixed 1024-channel assumption is still enforced.
        path = os.path.join(d, "n2.h5")
        tdt = np.dtype([("ctime", "f8")])
        with h5py.File(path, "w") as f:
            f.create_dataset("vis", data=np.zeros((1024, 3, 2), dtype=np.complex64))
            t = np.zeros(2, dtype=tdt)
            t["ctime"] = [100.0, 160.0]
            f.create_dataset("index_map/time", data=t)
        meta = OutriggerN2Reader().probe(path)
        assert meta["shape"] == (1024, 3, 2)
        assert meta["ctime_min"] == 100.0
        assert meta["ctime_max"] == 160.0

        bad = os.path.join(d, "bad.h5")
        with h5py.File(bad, "w") as f:
            f.create_dataset("vis", data=np.zeros((8, 3, 2), dtype=np.complex64))
        with pytest.raises(UnreadableUnitError, match="1024 freq channels"):
            OutriggerN2Reader().probe(bad)


def test_builtin_readers_mark_only_file_failures_unreadable(tmp_path):
    corrupt = tmp_path / "corrupt.h5"
    corrupt.write_bytes(b"not HDF5")
    baseband = ChimeBasebandReader()
    ctx = RunContext(instrument=load_instrument("chime"))
    with pytest.raises(UnreadableUnitError):
        baseband.probe(str(corrupt))
    with pytest.raises(UnreadableUnitError):
        list(baseband.iter_arrays(str(corrupt), ctx))

    gains_path = tmp_path / "gain_20250114T093512.5Z_cyga.h5"
    n2_path = tmp_path / "n2.h5"
    for path in (gains_path, n2_path):
        with h5py.File(path, "w"):
            pass
    gains = OutriggerGainsReader()
    with pytest.raises(UnreadableUnitError, match="gain"):
        gains.probe(str(gains_path))
    with pytest.raises(UnreadableUnitError):
        list(gains.iter_arrays(str(gains_path), ctx))

    n2 = OutriggerN2Reader()
    with pytest.raises(UnreadableUnitError, match="vis"):
        n2.probe(str(n2_path))
    with pytest.raises(UnreadableUnitError):
        list(n2.iter_arrays(str(n2_path), ctx))

    # Configuration errors remain run-level failures, never data dispositions.
    with pytest.raises(ValueError, match="freq_chunk"):
        list(n2.iter_arrays(str(n2_path), ctx, freq_chunk=0))


def test_n2_preflight_reflects_hdf5plugin():
    """preflight passes iff hdf5plugin imported; the message names the fix."""
    ok, notes = OutriggerN2Reader().preflight(ctx=None)
    if outrigger_n2_reader.hdf5plugin is None:
        assert ok is False and "hdf5plugin" in notes[0]
    else:
        assert ok is True and notes == []


if __name__ == "__main__":
    for fn in (test_both_readers_registered,
               test_gains_filename_parsing,
               test_n2_folder_parsing,
               test_gains_probe_on_synthetic_file,
               test_n2_probe_on_synthetic_file,
               test_n2_preflight_reflects_hdf5plugin):
        fn()
        print(f"PASSED: {fn.__name__}")
    print("ALL PASSED")
