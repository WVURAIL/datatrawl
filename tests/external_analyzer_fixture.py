"""Small analyzer loaded by the external-plugin integration test.

The fixture intentionally contains only plugin behavior that the test needs:
entry-point-free discovery, a declared stream contract, streaming accumulation,
and a meaning-changing ``--set`` value covered by the base resume manifest.
The installable, science-oriented example lives in
``WVURAIL/datatrawl-analyzer-template``.
"""
from __future__ import annotations

from numbers import Integral
from typing import Any, Iterable, Mapping

import numpy as np

from datatrawl.analyzer_base import AccumulatingAnalyzer
from datatrawl.interfaces import (
    EXPERIMENTAL,
    PluginInfo,
    RunContext,
    STREAM_COMPLEX_BASEBAND,
)
from datatrawl.registry import analyzer as register_analyzer

_ANALYSIS = "fixture-mean-power"
_NO_FRAME_CAP = -1


def _freq_id(spec: Any) -> int:
    """Resolve the fixture's deliberately narrow, one-channel selection."""
    if isinstance(spec, (list, tuple)) and len(spec) == 1:
        spec = spec[0]
    if isinstance(spec, bool):
        raise SystemExit("fixture-mean-power needs one integer --select freq_id")
    if isinstance(spec, Integral):
        value = int(spec)
    elif isinstance(spec, str) and spec.strip().isdigit():
        value = int(spec.strip())
    else:
        raise SystemExit("fixture-mean-power needs one integer --select freq_id")
    if value < 0:
        raise SystemExit("fixture-mean-power needs a non-negative --select freq_id")
    return value


@register_analyzer
class FixtureMeanPowerAnalyzer(AccumulatingAnalyzer):
    """Accumulate mean frame power and apply a test-only output scale."""

    info = PluginInfo(
        name=_ANALYSIS,
        kind="analyzer",
        summary="TEST FIXTURE: scaled mean power for external-plugin checks.",
        status=EXPERIMENTAL,
        instruments=("*",),
        produces="<freq_id>.npz",
        requires=("numpy",),
        accepts_stream_kinds=(STREAM_COMPLEX_BASEBAND,),
    )

    def __init__(self) -> None:
        super().__init__()
        self._power_sum = 0.0
        self._frame_count = 0
        self._scale = 1.0
        self._max_frames = _NO_FRAME_CAP

    def resolve_selection(self, ctx: RunContext, spec: Any) -> int:
        return _freq_id(spec)

    @staticmethod
    def _run_scale(ctx: RunContext) -> float:
        return float((ctx.options or {}).get("fixture_scale", 1.0))

    @staticmethod
    def _run_cap(ctx: RunContext) -> int:
        value = (ctx.options or {}).get("max_frames_per_file")
        return _NO_FRAME_CAP if value is None else int(value)

    def resume_parameters(self, ctx: RunContext) -> Mapping[str, Any]:
        """Keep only the two options that change this fixture's product."""
        return {
            "fixture_scale": self._run_scale(ctx),
            "max_frames_per_file": self._run_cap(ctx),
        }

    def begin(self, ctx: RunContext, first_meta: Mapping[str, Any]) -> None:
        self._scale = self._run_scale(ctx)
        self._max_frames = self._run_cap(ctx)

    def consume_file(self, arrays: Iterable, meta: Mapping[str, Any]) -> int:
        n = 0
        for frame in arrays:
            values = np.asarray(frame)
            self._power_sum += float(np.mean(np.abs(values) ** 2))
            self._frame_count += 1
            n += 1
        if n:
            self._record(meta)
        return n

    def _product(self) -> Mapping[str, Any]:
        mean = self._power_sum / self._frame_count if self._frame_count else 0.0
        return {
            "analysis": _ANALYSIS,
            "power_sum": self._power_sum,
            "frame_count": self._frame_count,
            "mean_power": mean * self._scale,
            "fixture_scale": self._scale,
            "max_frames_per_file": self._max_frames,
        }

    def _restore(self, z: Mapping[str, Any]) -> None:
        if str(z["analysis"]) != _ANALYSIS:
            raise SystemExit("existing product belongs to a different analyzer")
        self._power_sum = float(z["power_sum"])
        self._frame_count = int(z["frame_count"])
        self._scale = float(z["fixture_scale"])
        self._max_frames = int(z["max_frames_per_file"])

    def summary(self) -> Mapping[str, Any]:
        mean = self._power_sum / self._frame_count if self._frame_count else 0.0
        return {
            "frames": self._frame_count,
            "mean_power": round(mean * self._scale, 6),
        }
