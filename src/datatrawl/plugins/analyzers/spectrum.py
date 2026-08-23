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

A few things worth noting about how it handles --select:

  * It carries the ("*",) tag: needing only "blocks of samples," it runs against
    any telescope + reader.
  * --select must name explicit freq_ids (844, 614,706, 506-552); it cannot
    expand "all" (`_parse_freq_ids` explains why).
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
from typing import Any, Iterable, List, Mapping, Optional

import numpy as np

from ...interfaces import (RunContext, PluginInfo, READY,
                           STREAM_COMPLEX_BASEBAND)
from ...analyzer_base import AccumulatingAnalyzer
from ...instruments import nyquist_sign
from ...registry import analyzer as _register_analyzer
from ...selection import parse_freq_ids

_SIGNATURE = "spectrum"          # stamped into the product; verified on resume
_PRODUCT_SCHEMA = "datatrawl.spectrum/v1"
_HZ_PER_MHZ = 1_000_000.0
_CENTER_TOLERANCE_HZ = 1.0


def _parse_freq_ids(spec: Any, *, n_channels: Optional[int] = None) -> List[int]:
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
    parsed = parse_freq_ids(spec, n_channels=n_channels)
    if parsed is None:
        raise SystemExit(f"spectrum: --select {spec!r} resolved to no freq_ids.")
    return sorted(parsed)


@_register_analyzer
class PowerSpectrumAnalyzer(AccumulatingAnalyzer):
    _PRODUCT_SCHEMA = _PRODUCT_SCHEMA
    info = PluginInfo(
        name="spectrum",
        kind="analyzer",
        summary="Time- and feed-averaged power spectrum (PSD) per freq_id.",
        status=READY,
        instruments=("*",),
        produces="<freq_id>.npz (psd, psd_sum, count, freqs_hz, freqs_sky_hz, provenance)",
        requires=("numpy",),
        accepts_stream_kinds=(STREAM_COMPLEX_BASEBAND,),
        notes="General Hann-windowed averaged power spectrum (no absolute "
              "calibration) for complex baseband frames. Doubles as the copy-me "
              "example for a new analysis.",
    )

    def __init__(self) -> None:
        super().__init__()
        self._psd_sum = None          # [nfft] float64 running sum (fftshifted)
        self._count = 0               # frames summed
        self._nfft = 0                # taken from the first frame
        self._configured_nfft = 0     # reader framing requested for this run
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

    @staticmethod
    def _expected_center_hz(ctx: RunContext, freq_id: Optional[int]):
        """Instrument centre for a selected channel, when one is available."""
        if freq_id is None:
            return None
        instrument = getattr(ctx, "instrument", None)
        mapper = getattr(instrument, "freq_of_freq_id", None)
        if not callable(mapper):
            return None
        return float(mapper(freq_id)) * _HZ_PER_MHZ

    @staticmethod
    def _file_center_hz(value: Any, *, label: str) -> Optional[float]:
        """Return one finite file centre, preserving an explicitly missing value."""
        if value is None:
            return None
        try:
            center = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{label} reports invalid f_center_hz={value!r}") from exc
        if not np.isfinite(center):
            raise ValueError(
                f"{label} reports non-finite f_center_hz={value!r}")
        return center

    # -- selection: explicit freq_ids -> one resumable product each ----------
    def resolve_selection(self, ctx: RunContext, spec: Any) -> Any:
        return _parse_freq_ids(
            spec, n_channels=getattr(ctx.instrument, "n_channels", None))

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
        try:
            file_center = self._file_center_hz(fc, label="first file")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if self._resumed:
            # Never overwrite a resumed product's invariants; validate that the
            # first new file is consistent with them instead.
            if (file_center is not None and self._f_center is not None
                    and abs(file_center - self._f_center)
                    > _CENTER_TOLERANCE_HZ):
                raise SystemExit(
                    f"resumed product is freq_id {self._freq_id} "
                    f"(centre {self._f_center / _HZ_PER_MHZ:.4f} MHz) but the "
                    f"first new file is at "
                    f"{file_center / _HZ_PER_MHZ:.4f} MHz. "
                    "Use a fresh product.")
            return

        # Fresh start: capture the invariants this product is locked to.
        ch = self._expected_freq_id(ctx)
        self._freq_id = ch if ch is not None else -1
        self._fs = float(ctx.instrument.fs_hz)          # property, not a call
        self._nyquist_zone = int(getattr(ctx.instrument, "nyquist_zone", 1) or 1)
        self._configured_nfft = int(
            getattr(ctx.instrument, "nfft", 0) or 0)
        self._max_frames = self._run_cap(ctx)
        if file_center is not None:
            expected_center = self._expected_center_hz(ctx, ch)
            if (expected_center is not None
                    and abs(file_center - expected_center)
                    > _CENTER_TOLERANCE_HZ):
                raise SystemExit(
                    f"selected freq_id {ch} has instrument centre "
                    f"{expected_center / _HZ_PER_MHZ:.6f} MHz, but the first "
                    f"file reports {file_center / _HZ_PER_MHZ:.6f} MHz. "
                    "The file or inventory belongs to a different channel; "
                    "refusing to label its spectrum with the selected freq_id.")
            self._f_center = file_center
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
        try:
            file_center = self._file_center_hz(
                fc, label=str(meta.get("unit_name", "file")))
        except ValueError as exc:
            print(f"  skip {meta.get('unit_name', '?')}: {exc}", file=sys.stderr)
            return 0
        if (file_center is not None and self._f_center is not None
                and abs(file_center - self._f_center)
                > _CENTER_TOLERANCE_HZ):
            print(f"  skip {meta.get('unit_name', '?')}: f_center "
                  f"{file_center / _HZ_PER_MHZ:.4f} MHz != product "
                  f"{self._f_center / _HZ_PER_MHZ:.4f} MHz", file=sys.stderr)
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
        if n:
            # A unit is resumably complete only if at least one frame actually
            # contributed. Empty and wholly ragged files remain eligible for a
            # later retry instead of becoming permanent no-op provenance.
            self._record(meta)
        return n

    # -- resume / checkpoint -------------------------------------------------
    def resume(self, path: str, ctx: RunContext) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        if ("analysis" not in z.files) or (str(z["analysis"]) != _SIGNATURE):
            raise SystemExit(
                f"error: {path} was not written by the spectrum analyzer "
                f"(missing '{_SIGNATURE}' signature). Another analysis owns "
                f"this file -- point --out elsewhere so products don't mix.")
        if ("product_schema" not in z.files
                or str(z["product_schema"]) != _PRODUCT_SCHEMA):
            found = (str(z["product_schema"])
                     if "product_schema" in z.files else "missing")
            raise SystemExit(
                f"error: {path} has product_schema={found!r}, but the "
                f"spectrum analyzer requires {_PRODUCT_SCHEMA!r}. Refusing "
                "to continue a product with incompatible or unknown algorithm "
                "semantics; use a fresh output path.")

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
        product_center = float(z["f_center_hz"])
        expected_center = self._expected_center_hz(ctx, int(z["freq_id"]))
        if (np.isfinite(product_center) and expected_center is not None
                and abs(product_center - expected_center)
                > _CENTER_TOLERANCE_HZ):
            _mismatch("f_center_hz", product_center, expected_center)
        # ``nfft`` is the actual frame length supplied by the reader. It may be
        # different from the instrument's requested/default framing, so compare
        # the latter with its own stamp instead of rejecting valid products on
        # their observed frame length. Legacy products have no such stamp and
        # are checked against the first newly consumed frame instead.
        configured_now = int(getattr(ctx.instrument, "nfft", 0) or 0)
        configured_prev = (int(z["configured_nfft"])
                           if "configured_nfft" in z.files
                           else configured_now)
        if (configured_prev and configured_now
                and configured_prev != configured_now):
            _mismatch("configured_nfft", configured_prev, configured_now)
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
        self._configured_nfft = configured_prev
        self._freqs = np.array(z["freqs_hz"], dtype=np.float64)
        self._fs = fs_prev
        self._nyquist_zone = int(z["nyquist_zone"])
        self._f_center = product_center if np.isfinite(product_center) else None
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
            product_schema=_PRODUCT_SCHEMA,
            psd=psd,
            psd_sum=(self._psd_sum if self._psd_sum is not None else np.zeros(0)),
            count=self._count,
            freqs_hz=base,
            freqs_sky_hz=sky,
            f_center_hz=(self._f_center if self._f_center is not None else np.nan),
            freq_id=self._freq_id,
            nfft=self._nfft,
            configured_nfft=self._configured_nfft,
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
                                             + nyquist_sign(self._nyquist_zone) * f)
                                            / _HZ_PER_MHZ, 4)
            else:
                out["peak_hz"] = round(float(f), 1)
        return out
