"""
Analyzer: averaged power spectrum (PSD) per freq_id -- the worked example.

Read this top to bottom. It builds on AccumulatingAnalyzer (analyzer_base.py): the
base handles the crash-safe save, resume, and processed-key tracking, so a plain
analysis writes only begin()/consume_file()/_product()/_restore() -- see
docs/ADDING_AN_ANALYZER.md for that minimal shape. This analyzer is longer for one
reason: it overrides save()/resume() to stamp and *validate* per-product
invariants (freq_id, nfft, nyquist_zone), so a resume can never fold an incompatible run
into an existing product.

The DSP itself is a few lines of NumPy right here, with no imports beyond the
interfaces, so copying this file still gives a clean start for a new analysis.

What it produces, in one streaming pass over [nfft, n_feeds] complex frames:

    psd / psd_sum / count : feed-averaged |FFT|^2, time-averaged. psd_sum + count
                            are kept (not just the average) so a product resumes
                            exactly; `psd` is psd_sum/count for convenience.
    freqs_hz              : baseband axis (fftshifted, centred on 0).
    freqs_sky_hz          : sky axis = f_center +/- baseband, the sign set by the
                            instrument's Nyquist zone (nyquist_sign); this is where
                            the zone and the reader's `f_center_hz` earn their keep.

Two things worth noting about how it handles --select:

  * It carries the ("*",) tag: needing only "blocks of samples," it runs against
    any telescope + reader.
  * --select must name explicit freq_ids (844, 614,706, 506-552). A plain power
    spectrum has no notion of which freq_ids are "interesting," so it can't
    expand "all" -- that kind of selection model is something a detection
    analysis would add. The error message says so.
  * One product per freq_id: plan_runs() fans a multi-freq_id --select out to one
    resumable <freq_id>.npz each, because a single PSD only makes sense within
    one freq_id's band.

Run it (local source --- no archive credentials needed):

    datatrawl scan --telescope chime --source local --reader chime-baseband \
        --analyzer spectrum --source-root /path/to/baseband \
        --select 844 --max-files 3 --max-frames-per-file 8

or against an inventory you surveyed (telescope/source/reader come from its meta):

    datatrawl scan --inventory data/chime-spectrum/inventory.jsonl \
        --analyzer spectrum --select 844 --max-files 3 --max-frames-per-file 8

Product-name note: a single-freq_id product defaults to <freq_id>.npz, so if you
add another analysis that also names products per freq_id, give one of them its
own --out. As a backstop, resume() refuses to continue a product written by a
different analysis (it checks the `analysis` tag), so two can never silently
merge.
"""
from __future__ import annotations

import datetime
import os
import sys
from typing import Any, Iterable, List, Mapping

import numpy as np

from ...interfaces import RunContext, PluginInfo, READY
from ...analyzer_base import AccumulatingAnalyzer
from ...instruments import nyquist_sign
from ...registry import analyzer as _register_analyzer

_SIGNATURE = "spectrum"          # stamped into the product; verified on resume


def _parse_freq_ids(spec: Any) -> List[int]:
    """Resolve an explicit freq_id --select into a sorted list of ints.

    Accepts an int, a list/tuple/set of ints, or the strings "844", "614,706",
    "506-552" (inclusive range). There is no "all" expansion: a plain power
    spectrum has no model of which freq_ids matter, so the user must name them.
    """
    if spec is None or str(spec).strip().lower() in ("", "all", "*"):
        raise SystemExit(
            "spectrum needs explicit freq_id(s): --select 844 | 614,706 | "
            "506-552.\n('all' can't be expanded -- a power spectrum has no "
            "model of which freq_ids are interesting; name them explicitly.)")
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, (list, tuple, set)):
        return sorted(int(x) for x in spec)
    out: set[int] = set()
    try:
        for part in str(spec).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                out.update(range(int(lo), int(hi) + 1))
            else:
                out.add(int(part))
    except ValueError:
        raise SystemExit(
            f"spectrum: could not parse --select {spec!r} as freq_ids "
            f"(want e.g. 844 | 614,706 | 506-552).")
    if not out:
        raise SystemExit(f"spectrum: --select {spec!r} resolved to no freq_ids.")
    return sorted(out)


@_register_analyzer
class PowerSpectrumAnalyzer(AccumulatingAnalyzer):
    info = PluginInfo(
        name="spectrum",
        kind="analyzer",
        summary="Time- and feed-averaged power spectrum (PSD) per freq_id.",
        status=READY,
        instruments=("*",),
        produces="<freq_id>.npz (psd, psd_sum, count, freqs_hz, freqs_sky_hz, provenance)",
        requires=("numpy",),
        notes="General Hann-windowed averaged power spectrum (no absolute "
              "calibration). Doubles as the copy-me example for a new analysis.",
    )

    def __init__(self) -> None:
        super().__init__()
        self._psd_sum = None          # [nfft] float64 running sum (fftshifted)
        self._count = 0               # frames summed
        self._nfft = 0                # taken from the first frame
        self._window = None           # Hann, sized with nfft (xp array)
        self._freqs = None            # baseband axis (fftshifted)
        self._f_center = None         # Hz, channel centre (from the first file)
        self._fs = 0.0                # Hz, sample rate (from the instrument)
        self._nyquist_zone = 1        # Nyquist zone (from the instrument); sign via nyquist_sign
        self._freq_id = -1
        self._xp = np
        self._resumed = False         # True once resume() loaded a product
        self._max_frames = -1         # per-file cap this product was built with (-1=none)

    @staticmethod
    def _expected_freq_id(ctx: RunContext):
        """The single freq_id this run targets, or None if it isn't one-per-freq_id."""
        sel = ctx.selection
        if isinstance(sel, int):
            return int(sel)
        if isinstance(sel, (list, tuple)) and len(sel) == 1:
            return int(sel[0])
        return None

    @staticmethod
    def _run_cap(ctx: RunContext) -> int:
        v = (ctx.options or {}).get("max_frames_per_file")
        return int(v) if v else -1

    # -- selection: explicit freq_ids -> one resumable product each ----------
    def resolve_selection(self, ctx: RunContext, spec: Any) -> Any:
        return _parse_freq_ids(spec)

    def plan_runs(self, ctx: RunContext, spec: Any) -> list:
        return [[ch] for ch in self.resolve_selection(ctx, spec)]

    # -- lifecycle -----------------------------------------------------------
    def begin(self, ctx: RunContext, first_meta: Mapping[str, Any]) -> None:
        if (ctx.options or {}).get("gpu"):
            from datatrawl import accel
            self._xp = accel.get_array_module(True)
            if self._window is not None:                # re-home a resumed window
                self._window = self._xp.asarray(self._window)

        fc = first_meta.get("f_center_hz")
        if self._resumed:
            # Never overwrite a resumed product's invariants; validate that the
            # first new file is consistent with them instead.
            if (fc is not None and self._f_center is not None
                    and abs(float(fc) - self._f_center) > 1.0):
                raise SystemExit(
                    f"resumed product is freq_id {self._freq_id} "
                    f"(centre {self._f_center / 1e6:.4f} MHz) but the first new "
                    f"file is at {float(fc) / 1e6:.4f} MHz. Use a fresh product.")
            return

        # Fresh start: capture the invariants this product is locked to.
        ch = self._expected_freq_id(ctx)
        self._freq_id = ch if ch is not None else -1
        self._fs = float(ctx.instrument.fs_hz)          # property, not a call
        self._nyquist_zone = int(getattr(ctx.instrument, "nyquist_zone", 1) or 1)
        self._max_frames = self._run_cap(ctx)
        if fc is not None:
            self._f_center = float(fc)
        # nfft / window / freqs / accumulator are sized lazily on the first frame
        # so this analyzer works with any reader's frame length.

    def _size_to(self, nfft: int) -> None:
        self._nfft = int(nfft)
        win = np.hanning(self._nfft).astype(np.float64)   # default; swap to taste
        self._window = self._xp.asarray(win) if self._xp is not np else win
        d = (1.0 / self._fs) if self._fs else 1.0
        self._freqs = np.fft.fftshift(np.fft.fftfreq(self._nfft, d=d))
        self._psd_sum = np.zeros(self._nfft, dtype=np.float64)

    def consume_file(self, arrays: Iterable, meta: Mapping[str, Any]) -> int:
        xp = self._xp
        # one freq_id per product: never fold a different band into this PSD
        fc = meta.get("f_center_hz")
        if (fc is not None and self._f_center is not None
                and abs(float(fc) - self._f_center) > 1.0):
            print(f"  skip {meta.get('unit_name', '?')}: f_center "
                  f"{float(fc) / 1e6:.4f} MHz != product "
                  f"{self._f_center / 1e6:.4f} MHz", file=sys.stderr)
            return 0
        n = 0
        for frame in arrays:
            frame = xp.asarray(frame)
            if self._psd_sum is None:
                self._size_to(frame.shape[0])
            if frame.shape[0] != self._nfft:              # ragged frame -> skip
                continue
            w = self._window.reshape((-1,) + (1,) * (frame.ndim - 1))
            spec = xp.fft.fft(frame * w, axis=0)
            power = spec.real ** 2 + spec.imag ** 2       # |.|^2
            if power.ndim > 1:                            # average feeds/extra axes
                power = power.mean(axis=tuple(range(1, power.ndim)))
            shifted = xp.fft.fftshift(power)
            host = shifted if xp is np else xp.asnumpy(shifted)
            self._psd_sum += host.astype(np.float64)
            self._count += 1
            n += 1
        self._record(meta)
        return n

    # -- resume / checkpoint -------------------------------------------------
    def processed_keys(self) -> set:
        return set(self._keys)

    def resume(self, path: str, ctx: RunContext) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        if ("analysis" not in z.files) or (str(z["analysis"]) != _SIGNATURE):
            raise SystemExit(
                f"error: {path} was not written by the spectrum analyzer "
                f"(missing '{_SIGNATURE}' signature). Another analysis owns "
                f"this file -- point --out elsewhere so products don't mix.")

        # Refuse to continue a product built with different invariants -- otherwise
        # a resume silently folds an incompatible run into it (a different freq_id,
        # frame length, or Nyquist zone; or a capped smoke-test product).
        def _mismatch(label, was, now):
            raise SystemExit(
                f"error: {path} was built with {label}={was} but this run uses "
                f"{label}={now}. Use a fresh product (--out elsewhere).")

        fs_prev = float(z["fs_hz"])
        if abs(fs_prev - float(ctx.instrument.fs_hz)) > 1.0:
            _mismatch("fs_hz", fs_prev, float(ctx.instrument.fs_hz))
        exp_ch = self._expected_freq_id(ctx)
        if exp_ch is not None and int(z["freq_id"]) != exp_ch:
            _mismatch("freq_id", int(z["freq_id"]), exp_ch)
        inst_nfft = int(getattr(ctx.instrument, "nfft", 0) or 0)
        if inst_nfft and int(z["nfft"]) != inst_nfft:
            _mismatch("nfft", int(z["nfft"]), inst_nfft)
        inst_zone = int(getattr(ctx.instrument, "nyquist_zone", 0) or 0)
        if inst_zone and int(z["nyquist_zone"]) != inst_zone:
            _mismatch("nyquist_zone", int(z["nyquist_zone"]), inst_zone)
        prev_cap = (int(z["max_frames_per_file"])
                    if "max_frames_per_file" in z.files else -1)
        cur_cap = self._run_cap(ctx)
        if prev_cap != cur_cap:
            raise SystemExit(
                f"error: {path} was built with max_frames_per_file="
                f"{prev_cap if prev_cap >= 0 else 'none'} but this run uses "
                f"{cur_cap if cur_cap >= 0 else 'none'}. A capped smoke-test "
                f"product is not equivalent to a full one -- use --out elsewhere, "
                f"or delete it and rerun.")

        self._psd_sum = np.array(z["psd_sum"], dtype=np.float64)
        self._count = int(z["count"])
        self._nfft = int(z["nfft"])
        self._freqs = np.array(z["freqs_hz"], dtype=np.float64)
        self._fs = fs_prev
        self._nyquist_zone = int(z["nyquist_zone"])
        self._f_center = (float(z["f_center_hz"])
                          if np.isfinite(z["f_center_hz"]) else None)
        self._freq_id = int(z["freq_id"])
        self._max_frames = prev_cap
        self._keys = [str(x) for x in z["unit_keys"]]
        self._names = [str(x) for x in z["files"]]
        win = np.hanning(self._nfft).astype(np.float64)
        self._window = self._xp.asarray(win) if self._xp is not np else win
        self._resumed = True
        return True

    def save(self, path: str) -> None:
        base = self._freqs if self._freqs is not None else np.zeros(0)
        sky = (self._f_center + nyquist_sign(self._nyquist_zone) * base
               if self._f_center is not None else base)
        psd = (self._psd_sum / self._count
               if (self._psd_sum is not None and self._count)
               else (self._psd_sum if self._psd_sum is not None else np.zeros(0)))
        self._atomic_savez(
            path,
            analysis=_SIGNATURE,
            psd=psd,
            psd_sum=(self._psd_sum if self._psd_sum is not None else np.zeros(0)),
            count=self._count,
            freqs_hz=base,
            freqs_sky_hz=sky,
            f_center_hz=(self._f_center if self._f_center is not None else np.nan),
            freq_id=self._freq_id,
            nfft=self._nfft,
            fs_hz=self._fs,
            nyquist_zone=self._nyquist_zone,
            max_frames_per_file=self._max_frames,
            files=np.array(self._names),
            unit_keys=np.array(self._keys),
            created=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def summary(self) -> Mapping[str, Any]:
        out: dict = {"count": self._count, "files": len(self._names),
                     "freq_id": self._freq_id}
        if self._psd_sum is not None and self._count and self._freqs is not None:
            psd = self._psd_sum / self._count
            k = int(np.argmax(psd))
            f = self._freqs[k]
            if self._f_center is not None:
                out["peak_sky_mhz"] = round((self._f_center
                                             + nyquist_sign(self._nyquist_zone) * f) / 1e6, 4)
            else:
                out["peak_hz"] = round(float(f), 1)
        return out
