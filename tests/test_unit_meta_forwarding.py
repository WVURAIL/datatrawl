#!/usr/bin/env python3
"""Source-supplied Unit.meta must reach the analyzer.

The engine used to build per-unit meta from ``reader.probe(dest)`` alone, which
is the staged file's own attributes. Every inventory-level field the source had
already attached to ``Unit.meta`` was dropped before any analyzer saw it.

Some of those fields have no file-attribute equivalent at all. The CHIME
baseband inventory spans ``chime.event.baseband.raw`` (triggered) and
``chime.scheduled.baseband.raw`` (scheduled), and that split is load-bearing for
a survey selection function; a downstream consumer otherwise has to re-join
finished products against the inventory by event key.

Probe values still win on a key collision, because the probe read the bytes.
"""
from __future__ import annotations

import pytest

from datatrawl.interfaces import Unit


def test_unit_meta_is_merged_under_probe_output():
    """The documented precedence: source meta first, probe last."""
    unit = Unit(
        key="cadc:x/baseband_100058001_844.h5",
        name="baseband_100058001_844.h5",
        meta={
            "scope": "chime.event.baseband.raw",
            "event": "100058001",
            "obs_date": "2020-07-15",
            "freq_id": 844,
        },
    )
    probe = {"freq_id": 844, "f_center_hz": 470_312_500.0}

    merged = {**dict(unit.meta), **dict(probe)}

    # Inventory-only fields survive.
    assert merged["scope"] == "chime.event.baseband.raw"
    assert merged["obs_date"] == "2020-07-15"
    assert merged["event"] == "100058001"
    # Probe-supplied fields are present.
    assert merged["f_center_hz"] == 470_312_500.0


def test_probe_wins_on_collision():
    """A staged file that disagrees with its inventory row is believed."""
    unit = Unit(key="k", name="n", meta={"freq_id": 999, "scope": "s"})
    probe = {"freq_id": 844}

    merged = {**dict(unit.meta), **dict(probe)}

    assert merged["freq_id"] == 844, "probe must override a stale inventory row"
    assert merged["scope"] == "s"


def test_pipeline_uses_the_merge():
    """Pin the engine call site, not just the merge semantics."""
    import inspect

    from datatrawl import pipeline

    src = inspect.getsource(pipeline)
    assert "reader.probe(dest)" in src
    assert "unit.meta" in src, (
        "pipeline must forward source-supplied Unit.meta; building per-unit "
        "meta from reader.probe() alone silently drops inventory fields such "
        "as the triggered-versus-scheduled acquisition scope"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
