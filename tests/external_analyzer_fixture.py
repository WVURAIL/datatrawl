"""
Analyzer fixture for the external-plugin discovery tests.

`tests/test_external_plugin.py` loads this file via `--plugin <path>` and the
DATATRAWL_PLUGINS env var to prove datatrawl's external-plugin machinery: an
analyzer living outside `src/datatrawl/` is invisible until loaded, first-class
once loaded (list/doctor/scan through the full engine), honours a `--set`
parameter from `ctx.options` (here: `--set dc_mask_hz=...`), and is strict
about resume compatibility when that parameter or an engine-level invariant
changes.

This file is test scaffolding, kept out of pytest collection by its name. The
user-facing starting point for writing your own analyzer is the template
repository, which ships this same analyzer as an installable package whose
entry point makes it discoverable with no `--plugin` flag:

    https://github.com/WVURAIL/datatrawl-analyzer-template
"""
from __future__ import annotations

import datetime
import os
import tempfile
from typing import Any, Iterable, List, Mapping

import numpy as np

from datatrawl.interfaces import Analyzer, RunContext, PluginInfo, EXPERIMENTAL
from datatrawl.registry import analyzer as _register_analyzer
from datatrawl.instruments import nyquist_sign

_SIGNATURE = "freq_id-peak"


def _freq_ids(spec: Any) -> List[int]:
    if spec is None or str(spec).strip().lower() in ("", "all", "*"):
        raise SystemExit("freq_id-peak needs explicit freq_id(s): "
                         "--select 844 | 614,706 | 506-552")
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, (list, tuple, set)):
        return sorted(int(x) for x in spec)
    out: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return sorted(out)


@_register_analyzer
class FreqIdPeakAnalyzer(Analyzer):
    """Averaged power spectrum + its peak bin, one product per freq_id.

    A minimal but complete analyzer: it accumulates psd_sum/count in one streaming
    pass and, on save, reports the peak frequency (ignoring a configurable band
    around DC, `--set dc_mask_hz=...`).
    """
    info = PluginInfo(
        name="freq_id-peak",
        kind="analyzer",
        summary="EXAMPLE (external): averaged PSD + peak bin per freq_id.",
        status=EXPERIMENTAL,
        instruments=("*",),
        produces="<freq_id>.npz (psd_sum, count, peak_hz, peak_sky_hz, provenance)",
        requires=("numpy",),
        notes="Loaded via --plugin / DATATRAWL_PLUGINS / entry point; reads "
              "--set dc_mask_hz=<Hz>.",
    )

    def resolve_selection(self, ctx: RunContext, spec: Any) -> Any:
        return _freq_ids(spec)

    def plan_runs(self, ctx: RunContext, spec: Any) -> list:
        return [[ch] for ch in _freq_ids(spec)]

    @staticmethod
    def _expected_freq_id(ctx: RunContext):
        sel = ctx.selection
        if isinstance(sel, int):
            return int(sel)
        if isinstance(sel, (list, tuple)) and len(sel) == 1:
            return int(sel[0])
        return None

    @staticmethod
    def _run_cap(ctx: RunContext) -> int:
        value = (ctx.options or {}).get("max_frames_per_file")
        return int(value) if value else -1

    @staticmethod
    def _run_mask(ctx: RunContext) -> float:
        return float((ctx.options or {}).get("dc_mask_hz", 0.0) or 0.0)

    @staticmethod
    def _mismatch(path: str, label: str, saved, current) -> None:
        raise SystemExit(
            f"{path} was built with {label}={saved}, but this run uses "
            f"{label}={current}. Use a fresh product (--out elsewhere)."
        )

    def __init__(self) -> None:
        self._psd_sum = None
        self._count = 0
        self._keys: list = []
        self._files: list = []
        self._meta = {}
        self._dc_mask_hz = 0.0
        self._max_frames = -1

    # -- resume --------------------------------------------------------------
    def resume(self, path: str, ctx: RunContext) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        if "analysis" not in z.files or str(z["analysis"]) != _SIGNATURE:
            found = str(z["analysis"]) if "analysis" in z.files else "missing"
            raise SystemExit(f"{path} was written by analysis {found!r}, not "
                             f"{_SIGNATURE!r}; refusing to mix products. "
                             f"Use a different --out.")

        expected_freq_id = self._expected_freq_id(ctx)
        if expected_freq_id is not None and int(z["freq_id"]) != expected_freq_id:
            self._mismatch(path, "freq_id", int(z["freq_id"]), expected_freq_id)

        current_nfft = int(getattr(ctx.instrument, "nfft", 0) or 0)
        if current_nfft and int(z["nfft"]) != current_nfft:
            self._mismatch(path, "nfft", int(z["nfft"]), current_nfft)

        current_fs = float(ctx.instrument.fs_hz)
        if abs(float(z["fs_hz"]) - current_fs) > 1.0:
            self._mismatch(path, "fs_hz", float(z["fs_hz"]), current_fs)

        current_zone = int(getattr(ctx.instrument, "nyquist_zone", 1) or 1)
        if int(z["nyquist_zone"]) != current_zone:
            self._mismatch(path, "nyquist_zone", int(z["nyquist_zone"]), current_zone)

        saved_cap = (int(z["max_frames_per_file"])
                     if "max_frames_per_file" in z.files else -1)
        current_cap = self._run_cap(ctx)
        if saved_cap != current_cap:
            self._mismatch(
                path,
                "max_frames_per_file",
                saved_cap if saved_cap >= 0 else "none",
                current_cap if current_cap >= 0 else "none",
            )

        saved_mask = float(z["dc_mask_hz"])
        current_mask = self._run_mask(ctx)
        if abs(saved_mask - current_mask) > 1e-9:
            self._mismatch(path, "dc_mask_hz", saved_mask, current_mask)

        self._psd_sum = np.asarray(z["psd_sum"], dtype=np.float64)
        self._count = int(z["count"])
        self._keys = [str(x) for x in np.asarray(z["unit_keys"]).tolist()]
        self._files = [str(x) for x in np.asarray(z["files"]).tolist()]
        self._meta = {"nfft": int(z["nfft"]), "fs_hz": float(z["fs_hz"]),
                      "f_center_hz": float(z["f_center_hz"]),
                      "nyquist_zone": int(z["nyquist_zone"]),
                      "freq_id": int(z["freq_id"])}
        self._dc_mask_hz = saved_mask
        self._max_frames = saved_cap
        return True

    def processed_keys(self) -> set:
        return set(self._keys)

    # -- lifecycle -----------------------------------------------------------
    def begin(self, ctx: RunContext, first_meta: Mapping[str, Any]) -> None:
        f_center = first_meta.get("f_center_hz")
        if self._meta:
            if (f_center is not None
                    and abs(float(f_center) - self._meta["f_center_hz"]) > 1.0):
                self._mismatch(
                    "resumed product",
                    "f_center_hz",
                    self._meta["f_center_hz"],
                    float(f_center),
                )
            return

        ch = self._expected_freq_id(ctx)
        self._meta = {
            "nfft": int(getattr(ctx.instrument, "nfft", 0)),
            "fs_hz": float(ctx.instrument.fs_hz),
            "f_center_hz": float(f_center or 0.0),
            "nyquist_zone": int(getattr(ctx.instrument, "nyquist_zone", 1)),
            "freq_id": ch if ch is not None else -1,
        }
        self._dc_mask_hz = self._run_mask(ctx)
        self._max_frames = self._run_cap(ctx)

    def consume_file(self, arrays: Iterable, meta: Mapping[str, Any]) -> int:
        n = 0
        for frame in arrays:
            x = np.asarray(frame)
            expected_nfft = int(self._meta.get("nfft", 0) or 0)
            if expected_nfft and x.shape[0] != expected_nfft:
                raise SystemExit(
                    f"{meta.get('unit_name', '?')} has frame length {x.shape[0]}, "
                    f"but this product uses nfft={expected_nfft}."
                )
            w = np.hanning(x.shape[0]).astype(np.float64)
            w = w.reshape((-1,) + (1,) * (x.ndim - 1))
            X = np.fft.fft(x * w, axis=0)
            power = X.real**2 + X.imag**2
            if power.ndim > 1:
                power = power.mean(axis=tuple(range(1, power.ndim)))
            p = np.fft.fftshift(power)
            self._psd_sum = p if self._psd_sum is None else self._psd_sum + p
            self._count += 1
            n += 1
        self._keys.append(meta.get("unit_key"))
        self._files.append(meta.get("unit_name"))
        return n

    # -- save ----------------------------------------------------------------
    def _freqs_hz(self) -> np.ndarray:
        nfft, fs = self._meta["nfft"], self._meta["fs_hz"]
        return np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))

    def save(self, path: str) -> None:
        freqs = self._freqs_hz()
        psd_sum = (self._psd_sum if self._psd_sum is not None
                   else np.zeros_like(freqs))
        psd = psd_sum / max(self._count, 1)
        masked = psd.copy()
        if self._dc_mask_hz > 0:
            masked[np.abs(freqs) < self._dc_mask_hz] = -np.inf
        k = int(np.argmax(masked))
        f_center, nz = self._meta["f_center_hz"], self._meta["nyquist_zone"]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".npz", dir=os.path.dirname(path) or ".")
        os.close(fd)
        try:
            np.savez_compressed(
                tmp,
                analysis=_SIGNATURE,
                psd_sum=psd_sum, count=self._count,
                freqs_hz=freqs, peak_hz=float(freqs[k]),
                peak_sky_hz=float(f_center + nyquist_sign(nz) * freqs[k]),
                f_center_hz=f_center, freq_id=self._meta["freq_id"],
                nfft=self._meta["nfft"], fs_hz=self._meta["fs_hz"], nyquist_zone=nz,
                max_frames_per_file=self._max_frames,
                dc_mask_hz=self._dc_mask_hz,
                files=np.array(self._files), unit_keys=np.array(self._keys),
                created=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def summary(self) -> dict:
        freqs = self._freqs_hz() if self._meta else np.zeros(1)
        psd = (self._psd_sum / max(self._count, 1)) if self._psd_sum is not None \
            else np.zeros_like(freqs)
        masked = psd.copy()
        if self._dc_mask_hz > 0 and masked.size == freqs.size:
            masked[np.abs(freqs) < self._dc_mask_hz] = -np.inf
        k = int(np.argmax(masked)) if masked.size else 0
        f_center = self._meta.get("f_center_hz", 0.0)
        nz = self._meta.get("nyquist_zone", 1)
        return {"count": self._count, "files": len(self._files),
                "freq_id": self._meta.get("freq_id"),
                "peak_sky_mhz": round((f_center + nyquist_sign(nz) * freqs[k]) / 1e6, 4)
                if self._meta else None}
