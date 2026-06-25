#!/usr/bin/env python3
"""
Tests for datatrawl.accel -- CuPy resolution for GPU analyzers.

The interesting logic is pure and offline: parsing a CUDA version out of
nvidia-smi/nvcc text, naming the matching wheel, and falling back cleanly when
CuPy is absent. The CPU path (numpy) and the "no cupy -> actionable error" path
are exercised here; the actual install is not (that needs a CUDA image).

Run:  PYTHONPATH=src python tests/test_accel.py
"""
from __future__ import annotations

import numpy as np

from datatrawl import accel


def test_cuda_major_parsing():
    # nvidia-smi header form
    assert accel._cuda_major_from_text("...\nCUDA Version: 12.4    |\n...") == 12
    # nvcc --version form
    assert accel._cuda_major_from_text("Cuda compilation tools, release 11.8, V11.8.89") == 11
    # nothing parseable
    assert accel._cuda_major_from_text("no version here") is None


def test_cupy_package_name():
    assert accel.cupy_package(12) == "cupy-cuda12x"
    assert accel.cupy_package(11) == "cupy-cuda11x"


def test_get_array_module_cpu_is_numpy():
    assert accel.get_array_module(False) is np


def test_gpu_path_matches_cupy_availability():
    cp = accel.import_cupy()
    if cp is None:
        # no cupy in this env: the scan path must fail cleanly, not ImportError
        raised = False
        try:
            accel.get_array_module(True)
        except SystemExit as exc:
            raised = True
            assert "setup-cupy" in str(exc)
        assert raised, "get_array_module(True) should SystemExit when cupy is absent"
        # and ensure_cupy without install should raise an actionable RuntimeError
        try:
            accel.ensure_cupy(install=False)
        except RuntimeError as exc:
            assert "setup-cupy" in str(exc)
        else:
            raise AssertionError("ensure_cupy(install=False) should raise when cupy is absent")
    else:
        # cupy present (e.g. a CUDA image): use it, and ensure_cupy is a no-op return
        assert accel.get_array_module(True) is cp
        assert accel.ensure_cupy(install=False) is cp


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print(f"  ok: {fn}")
    print("GPU MODULE TESTS PASSED")
