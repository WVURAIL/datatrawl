#!/usr/bin/env python3
"""
Tests for instrument config resolution: `extends:` inheritance and `scopes`.

These cover the data-as-config behaviour that the CHIME outriggers rely on -- a
station that shares CHIME's channelization is a few lines (`extends: chime` plus
the feed count and its scope) instead of a near-duplicate YAML -- and the scope
list that drives the survey default. They run fully offline.

Run:  PYTHONPATH=src python tests/test_instruments_extends.py
"""
from __future__ import annotations

import os
import tempfile

from datatrawl import instruments as inst_mod


# ---------------------------------------------------------------------------
# extends: shipped outriggers inherit CHIME geometry, override only what differs
# ---------------------------------------------------------------------------
def test_outrigger_inherits_chime_geometry():
    chime = inst_mod.load_instrument("chime")
    for name in ("gbo", "hco", "kko"):
        o = inst_mod.load_instrument(name)
        # inherited, untouched geometry
        assert o.f0_mhz == chime.f0_mhz
        assert o.bandwidth_mhz == chime.bandwidth_mhz
        assert o.n_channels == chime.n_channels
        assert o.nyquist_zone == chime.nyquist_zone
        assert o.nfft == chime.nfft
        assert o.reader == chime.reader
        # overridden, station-specific fields
        assert o.name == name
        assert o.n_feeds == 256 and chime.n_feeds == 2048
        assert o.scopes == (f"{name}.event.baseband.raw",)
        # any frequency<->freq_id mapping therefore matches CHIME's
        assert o.freq_of_freq_id(844) == chime.freq_of_freq_id(844)


def _write(d, name, text):
    with open(os.path.join(d, f"{name}.yaml"), "w") as f:
        f.write(text)


def test_extends_overrides_and_deep_merges():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "base",
               "name: base\n"
               "band: {f0_mhz: 800.0, bandwidth_mhz: 400.0, n_channels: 1024, descending: true}\n"
               "nyquist_zone: 2\n"
               "n_feeds: 2048\n"
               "nfft: 16384\n"
               "scopes: [base.scope]\n"
               "reader: chime-baseband\n")
        # child overrides n_feeds + scopes + one band key, inherits the rest
        _write(d, "child",
               "name: child\n"
               "extends: base\n"
               "n_feeds: 256\n"
               "band: {n_channels: 2048}\n"
               "scopes: [child.scope]\n")
        c = inst_mod.load_instrument("child", directory=d)
        assert c.name == "child"
        assert c.n_feeds == 256                     # overridden
        assert c.scopes == ("child.scope",)         # overridden
        assert c.n_channels == 2048                 # deep-merged band key
        assert c.f0_mhz == 800.0                    # inherited band key
        assert c.nyquist_zone == 2 and c.nfft == 16384  # inherited scalars
        assert c.reader == "chime-baseband"         # inherited


def test_extends_cycle_raises():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a", "name: a\nextends: b\nband: {f0_mhz: 1, bandwidth_mhz: 1, n_channels: 1}\nnyquist_zone: 1\n")
        _write(d, "b", "name: b\nextends: a\nband: {f0_mhz: 1, bandwidth_mhz: 1, n_channels: 1}\nnyquist_zone: 1\n")
        try:
            inst_mod.load_instrument("a", directory=d)
        except ValueError as exc:
            assert "circular" in str(exc).lower()
        else:
            raise AssertionError("expected a ValueError on a circular extends chain")


# ---------------------------------------------------------------------------
# scopes: accept a list or a comma-separated string; drive readiness
# ---------------------------------------------------------------------------
def test_scopes_accepts_list_and_csv():
    with tempfile.TemporaryDirectory() as d:
        body = ("band: {f0_mhz: 800.0, bandwidth_mhz: 400.0, n_channels: 1024}\n"
                "nyquist_zone: 2\n")
        _write(d, "as_list", "name: as_list\n" + body + "scopes: [a.one, a.two]\n")
        _write(d, "as_csv",  "name: as_csv\n"  + body + "scopes: 'b.one, b.two'\n")
        assert inst_mod.load_instrument("as_list", directory=d).scopes == ("a.one", "a.two")
        assert inst_mod.load_instrument("as_csv",  directory=d).scopes == ("b.one", "b.two")


def test_readiness_tracks_scopes():
    # shipped chime declares scopes -> ready
    assert inst_mod.instrument_readiness("chime").ready is True
    with tempfile.TemporaryDirectory() as d:
        _write(d, "noscope",
               "name: noscope\n"
               "band: {f0_mhz: 800.0, bandwidth_mhz: 400.0, n_channels: 1024}\n"
               "nyquist_zone: 2\n")          # geometry but no scopes
        rd = inst_mod.instrument_readiness("noscope", directory=d)
        assert rd.nyquist_zone_set is True and rd.scopes_set is False
        assert rd.status == "geometry-only"


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print(f"  ok: {fn}")
    print("INSTRUMENTS EXTENDS/SCOPES TESTS PASSED")
