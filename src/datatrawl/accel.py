"""
datatrawl.accel -- resolve the CuPy build for GPU analyzers.

Policy: prefer the CuPy the CANFAR session image already ships. A pinned cupy in
this package would shadow or mismatch the image's CUDA module, so the GPU extra is
empty and this module does the right thing at run time instead:

  * `import_cupy()`        -> the image's cupy, or None if it isn't importable.
  * `get_array_module(g)`  -> numpy, or the image's cupy when g is true (clean
                              error pointing at datatrawl setup-cupy, not a raw ImportError).
  * `detect_cuda_major()`  -> the image's CUDA major version, best-effort.
  * `ensure_cupy(install)` -> the image's cupy, optionally pip-installing the
                              matching `cupy-cuda<major>x` wheel when it is absent.

Auto-install lives only in `ensure_cupy(install=True)` (driven by the
`datatrawl setup-cupy --install` script). A scan never pip-installs on its own.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional


def import_cupy():
    """Return the cupy module provided by the environment/image, or None."""
    try:
        import cupy as cp  # noqa: F401
        return cp
    except Exception:
        return None


def _cuda_major_from_text(text: str) -> Optional[int]:
    m = re.search(r"CUDA Version[:\s]+(\d+)\.", text)          # nvidia-smi header
    if m:
        return int(m.group(1))
    m = re.search(r"release\s+(\d+)\.", text)                  # nvcc --version
    if m:
        return int(m.group(1))
    return None


def detect_cuda_major() -> Optional[int]:
    """Best-effort CUDA major version of the running image.

    Tries, in order: nvidia-smi, nvcc --version, then the CUDA version file under
    $CUDA_HOME / /usr/local/cuda. Returns None if nothing reports a version.
    """
    for cmd in (["nvidia-smi"], ["nvcc", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        major = _cuda_major_from_text((out.stdout or "") + (out.stderr or ""))
        if major:
            return major

    cuda_home = os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH") \
        or "/usr/local/cuda"
    for fname in ("version.json", "version.txt"):
        path = os.path.join(cuda_home, fname)
        try:
            with open(path) as fh:
                major = _cuda_major_from_text(fh.read())
                if major:
                    return major
        except OSError:
            continue
    return None


def cupy_package(major: int) -> str:
    """The pip wheel name for a given CUDA major version, e.g. 12 -> cupy-cuda12x."""
    return f"cupy-cuda{int(major)}x"


def ensure_cupy(install: bool = False, quiet: bool = False):
    """Return the image's cupy, optionally installing the matching wheel.

    If cupy is already importable (the common CANFAR case), it is returned as-is.
    Otherwise, when install=True, the image's CUDA major version is detected and
    `cupy-cuda<major>x` is pip-installed into the active environment, then imported.
    Raises RuntimeError with an actionable message if it cannot be resolved.
    """
    cp = import_cupy()
    if cp is not None:
        return cp

    if not install:
        raise RuntimeError(
            "cupy is not importable in this environment. Run "
            "`datatrawl setup-cupy --install` to detect this image's CUDA version "
            "and install the matching cupy wheel.")

    major = detect_cuda_major()
    if major is None:
        raise RuntimeError(
            "cupy is missing and the CUDA version could not be detected "
            "(no nvidia-smi/nvcc and no CUDA version file). Install the cupy build "
            "matching your session image manually, e.g. `pip install cupy-cuda12x`.")

    pkg = cupy_package(major)
    if not quiet:
        print(f"[gpu] no cupy found; detected CUDA {major}.x -> installing {pkg}",
              flush=True)
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--break-system-packages", pkg])

    cp = import_cupy()
    if cp is None:
        raise RuntimeError(
            f"installed {pkg} but cupy is still not importable; the wheel may not "
            f"match this image's CUDA module. Check `nvidia-smi` and install by hand.")
    return cp


def get_array_module(use_gpu: bool):
    """numpy, or the image's cupy when use_gpu is true.

    A scan path calls this; a missing cupy raises a clean SystemExit pointing at
    datatrawl setup-cupy rather than letting a bare `import cupy` ImportError surface.
    """
    import numpy as np
    if not use_gpu:
        return np
    cp = import_cupy()
    if cp is None:
        raise SystemExit(
            "--gpu was requested but cupy is not importable. Run "
            "`datatrawl setup-cupy --install` first (or drop --gpu to run on CPU).")
    return cp
