"""
Worked external-analyzer example:
an analyzer that lives OUTSIDE src/datatrawl/, the way a user's own analysis would.
test_external_plugin.py loads it to prove the external-plugin path works; it doubles as a 
concrete reference for the pattern in docs/ADDING_AN_ANALYZER.md.

It is loaded into datatrawl at runtime via any of:

    datatrawl scan --plugin /path/to/your_analyzer.py ...                   # path
    datatrawl scan --plugin mypkg.your_analyzer ...                         # module
    DATATRAWL_PLUGINS=/path/to/your_analyzer.py datatrawl ...               # env
    # or an entry point in your package's pyproject.toml:
    #   [project.entry-points."datatrawl.plugins"]
    #   freq_id-peak = "mypkg.your_analyzer"

Once loaded, `@analyzer` registers it and it is first-class: it shows up in
`datatrawl list analyzers` / `doctor` and runs through the full engine (per-freq_id
fan-out, dedup, quarantine, self-heal/resume, checkpointing) exactly like a
built-in. This is how you keep your science in your own repo while still using the
shared tool's machinery -- a real analysis (e.g. an F-statistic detector) follows
the same shape.

It also shows reading an analyzer-specific parameter from ctx.options, set on the
command line with `--set key=value` (here: `--set dc_mask_hz=...`).
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

    def __init__(self) -> None:
        self._psd_sum = None
        self._count = 0
        self._keys: list = []
        self._files: list = []
        self._meta = {}
        self._dc_mask_hz = 0.0

    # -- resume --------------------------------------------------------------
    def resume(self, path: str, ctx: RunContext) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        if str(z.get("analysis")) != _SIGNATURE:
            raise SystemExit(f"{path} was written by analysis "
                             f"{str(z.get('analysis'))!r}, not {_SIGNATURE!r}; "
                             f"refusing to mix products. Use a different --out.")
        self._psd_sum = np.asarray(z["psd_sum"], dtype=np.float64)
        self._count = int(z["count"])
        self._keys = list(np.asarray(z["unit_keys"]).tolist())
        self._files = list(np.asarray(z["files"]).tolist())
        self._meta = {"nfft": int(z["nfft"]), "fs_hz": float(z["fs_hz"]),
                      "f_center_hz": float(z["f_center_hz"]),
                      "nyquist_zone": int(z["nyquist_zone"]), "freq_id": int(z["freq_id"])}
        return True

    def processed_keys(self) -> set:
        return set(self._keys)

    # -- lifecycle -----------------------------------------------------------
    def begin(self, ctx: RunContext, first_meta: Mapping[str, Any]) -> None:
        if not self._meta:
            ch = ctx.selection[0] if isinstance(ctx.selection, (list, tuple)) \
                else ctx.selection
            self._meta = {
                "nfft": int(getattr(ctx.instrument, "nfft", 0)),
                "fs_hz": float(ctx.instrument.fs_hz),
                "f_center_hz": float(first_meta.get("f_center_hz", 0.0)),
                "nyquist_zone": int(getattr(ctx.instrument, "nyquist_zone", 1)),
                "freq_id": int(ch) if ch is not None else -1,
            }
        self._dc_mask_hz = float((ctx.options or {}).get("dc_mask_hz", 0.0) or 0.0)

    def consume_file(self, arrays: Iterable, meta: Mapping[str, Any]) -> int:
        n = 0
        for frame in arrays:
            x = np.asarray(frame)                      # [nfft, n_feeds] complex
            w = np.hanning(x.shape[0]).astype(np.float64)
            X = np.fft.fft(x * w[:, None], axis=0)
            p = np.fft.fftshift((X.real**2 + X.imag**2).mean(axis=1))
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
        psd = self._psd_sum / max(self._count, 1)
        masked = psd.copy()
        if self._dc_mask_hz > 0:
            masked[np.abs(freqs) < self._dc_mask_hz] = -np.inf
        k = int(np.argmax(masked))
        f_center, nz = self._meta["f_center_hz"], self._meta["nyquist_zone"]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".npz", dir=os.path.dirname(path) or ".")
        os.close(fd)
        np.savez_compressed(
            tmp,
            analysis=_SIGNATURE,
            psd_sum=self._psd_sum, count=self._count,
            freqs_hz=freqs, peak_hz=float(freqs[k]),
            peak_sky_hz=float(f_center + nyquist_sign(nz) * freqs[k]),
            f_center_hz=f_center, freq_id=self._meta["freq_id"],
            nfft=self._meta["nfft"], fs_hz=self._meta["fs_hz"], nyquist_zone=nz,
            dc_mask_hz=self._dc_mask_hz,
            files=np.array(self._files), unit_keys=np.array(self._keys),
            created=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        os.replace(tmp, path)

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
