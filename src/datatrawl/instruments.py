"""
datatrawl.instruments -- per-telescope channelization, as data not code.

A telescope is a YAML file under instruments/. It describes the band geometry
(freq_id 0 frequency, processed bandwidth, channel count, ordering, Nyquist zone)
and the Datatrail baseband `scopes` its data lives under. Any frequency<->freq_id
mapping falls out of this geometry, so adding a telescope that shares an existing
channelization is a YAML entry (often just `extends:` plus a feed count and scope),
not a code change.

Nyquist zone is a telescope property. Odd zones use the normal baseband direction;
even zones are inverted. Current CHIME-family instruments in this repository use
the second Nyquist zone.

Which freq_ids are actually occupied is geography, not configuration, and is
discovered by a survey, not declared here.

This module is intentionally analysis-agnostic: what a given analysis looks for
(a particular carrier, an RFI model, a feed list) is a property of the analyzer,
not the telescope. The instrument is pure geometry plus data access metadata.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import List

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

_INSTRUMENT_DIR = os.path.join(os.path.dirname(__file__), "instruments")


# --------------------------------------------------------------------------
# Instrument geometry
# --------------------------------------------------------------------------
@dataclass
class Instrument:
    name: str
    f0_mhz: float          # sky frequency at freq_id 0
    bandwidth_mhz: float   # total processed bandwidth
    n_channels: int        # number of frequency channels
    descending: bool       # True if higher freq_id => lower frequency (CHIME)
    nyquist_zone: int      # 1 = normal baseband direction, 2 = inverted (the
                           #   sign of the baseband->sky mapping; see nyquist_sign)
    n_feeds: int           # feeds/inputs incoherently combined
    nfft: int = 16384      # default analysis FFT length per frame
    scopes: tuple[str, ...] = ()  # datatrail baseband scope(s) this station registers;
                           # the default survey scope(s) when --scope is omitted.
    reader: str = ""       # canonical reader for this telescope
                           # (e.g. "chime-baseband"); the default `scan` uses
                           # when --reader is omitted. Pure data, like the rest.

    @property
    def fs_hz(self) -> float:
        """Per-channel (complex) sample rate = channel spacing."""
        return self.bandwidth_mhz * 1e6 / self.n_channels

    @property
    def chan_step_mhz(self) -> float:
        sign = -1.0 if self.descending else 1.0
        return sign * self.bandwidth_mhz / self.n_channels

    def freq_of_freq_id(self, n: float) -> float:
        """Channel-center sky frequency (MHz) for freq_id n."""
        return self.f0_mhz + n * self.chan_step_mhz

    def freq_id_of_freq(self, f_mhz: float) -> int:
        """Nearest freq_id containing sky frequency f_mhz."""
        return int(round((f_mhz - self.f0_mhz) / self.chan_step_mhz))


def nyquist_sign(nyquist_zone: int) -> int:
    """The +1/-1 baseband->sky direction implied by a Nyquist zone.

    Odd zones keep the baseband direction (sky = f_center + baseband); even zones
    invert it (sky = f_center - baseband). This is the one place that rule lives.
    """
    return 1 if int(nyquist_zone) % 2 else -1


# --------------------------------------------------------------------------
# Loading + discovery
# --------------------------------------------------------------------------
def list_instrument_names(directory: str | None = None) -> List[str]:
    directory = directory or _INSTRUMENT_DIR
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(directory, "*.yaml")))


def _load_yaml(name: str, directory: str | None = None) -> dict:
    if yaml is None:
        raise RuntimeError("pyyaml not installed; "
                           "pip install pyyaml --break-system-packages")
    directory = directory or _INSTRUMENT_DIR
    path = os.path.join(directory, f"{name}.yaml")
    if not os.path.exists(path):
        opts = ", ".join(list_instrument_names(directory)) or "(none)"
        raise FileNotFoundError(f"no instrument config {name!r}. Available: {opts}")
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, over: dict) -> dict:
    """Merge `over` onto `base`. Nested dicts merge key-by-key; every other value
    (scalars, lists) is replaced wholesale by `over`."""
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_config(name: str, directory: str | None = None,
                    _seen: set | None = None) -> dict:
    """Load <name>.yaml, applying `extends: <parent>` inheritance.

    A telescope that shares another's geometry sets `extends: <parent>` and lists
    only what differs (feed count, scope, name); the parent's config is loaded
    first and the child's keys override it. This is what lets the CHIME outriggers
    be a few lines each instead of a near-duplicate of chime.yaml.
    """
    _seen = set() if _seen is None else _seen
    if name in _seen:
        chain = " -> ".join(list(_seen) + [name])
        raise ValueError(f"instrument '{name}': circular extends chain ({chain})")
    _seen.add(name)
    cfg = _load_yaml(name, directory)
    parent = cfg.get("extends")
    if parent:
        base = _resolve_config(str(parent), directory, _seen)
        cfg = _deep_merge(base, {k: v for k, v in cfg.items() if k != "extends"})
    return cfg


def _coerce_scopes(value) -> tuple[str, ...]:
    """Accept a list or a comma-separated string of datatrail scopes."""
    if not value:
        return ()
    if isinstance(value, str):
        value = value.split(",")
    return tuple(str(s).strip() for s in value if str(s).strip())


def load_instrument(name: str, directory: str | None = None) -> Instrument:
    """Load <name>.yaml (resolving `extends:`) into an Instrument.

    Configuration uses `nyquist_zone`; a legacy `sense:` value is still accepted and
    mapped to the equivalent zone. The +1/-1 spectral direction is derived on demand
    via `nyquist_sign(instrument.nyquist_zone)`.
    """
    cfg = _resolve_config(name, directory)
    band = cfg["band"]
    nyquist_zone = cfg.get("nyquist_zone")
    if nyquist_zone is None:
        # Legacy configs may still carry `sense`; map it to the equivalent zone.
        sense_cfg = cfg.get("sense")
        if sense_cfg is None:
            raise ValueError(
                f"instrument '{name}': nyquist_zone is unset. Set nyquist_zone: 1 "
                f"for normal baseband direction or nyquist_zone: 2 for inverted.")
        sense_int = int(sense_cfg)
        nyquist_zone = 1 if sense_int > 0 else 2
    nyquist_zone = int(nyquist_zone)
    if nyquist_zone < 1:
        raise ValueError(f"instrument '{name}': nyquist_zone must be >= 1")
    return Instrument(
        name=cfg["name"],
        f0_mhz=float(band["f0_mhz"]),
        bandwidth_mhz=float(band["bandwidth_mhz"]),
        n_channels=int(band["n_channels"]),
        descending=bool(band.get("descending", True)),
        nyquist_zone=nyquist_zone,
        n_feeds=int(cfg.get("n_feeds", 0)),
        nfft=int(cfg.get("nfft", 16384)),
        scopes=_coerce_scopes(cfg.get("scopes")),
        reader=cfg.get("reader", ""),
    )


@dataclass(frozen=True)
class Readiness:
    """At-a-glance 'can I use this telescope yet?' summary for `list telescopes`."""
    name: str
    nyquist_zone_set: bool
    scopes_set: bool

    @property
    def ready(self) -> bool:
        # Usable out of the box: geometry + a built-in default survey scope.
        return self.nyquist_zone_set and self.scopes_set

    @property
    def status(self) -> str:
        if self.ready:
            return "ready"
        if self.nyquist_zone_set:
            return "geometry-only"   # geometry known, but no built-in scope -> pass --scope
        return "stub"

    def missing(self) -> List[str]:
        out = []
        if not self.nyquist_zone_set:
            out.append("nyquist_zone")
        if not self.scopes_set:
            out.append("scopes")
        return out

    def usable_for(self, needs_archive_config: bool) -> bool:
        """Can this telescope run with a source of the given kind?

        A local source needs only geometry + Nyquist zone. An archive source can
        survey any geometry-ready telescope, but `doctor`'s ready-combos require a
        built-in scope so they work with no extra args; a geometry-only telescope
        still works against an archive source if you pass --scope explicitly.
        """
        if needs_archive_config:
            return self.ready
        return self.nyquist_zone_set


def instrument_readiness(name: str, directory: str | None = None) -> Readiness:
    cfg = _resolve_config(name, directory)
    return Readiness(
        name=cfg.get("name", name),
        nyquist_zone_set=(cfg.get("nyquist_zone") is not None or cfg.get("sense") is not None),
        scopes_set=bool(_coerce_scopes(cfg.get("scopes"))),
    )


def all_readiness(directory: str | None = None) -> List[Readiness]:
    out = []
    for name in list_instrument_names(directory):
        try:
            out.append(instrument_readiness(name, directory))
        except Exception:
            continue
    return out
