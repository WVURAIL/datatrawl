"""
Shared selection parsing for sources -- freq_ids AND events, one grammar.

Every source interprets `ctx.selection` ("what the user asked to process").
Historically that meant freq_ids only, which quietly hard-wired one access
pattern -- per-freq_id, across all events -- into the selection contract. An
event-oriented analysis (e.g. beamforming every freq_id of ONE event into a
single product) could not tell a source "give me event E": its `plan_runs`
sub-selection would be misparsed as a freq_id. `event` has been a column in
every archive inventory row from the start; this module makes it selectable.

Accepted forms (a source passes whatever it received straight to
`parse_selection`):

    None / "" / "all" / "*"        -> no filter (every unit)
    844 / [614, 706] / "614,706"
      / "506-844"                  -> freq_ids, exactly as before
    "events:349382977,352918475"
      / "event:349382977"          -> events (string prefix form, CLI-friendly)
    {"freq_ids": <any form above>,
     "events":  [349382977, ...]}  -> both filters, ANDed (the programmatic
                                      form a `plan_runs` returns)

Two deliberate rules:

  * An event filter is always EXPLICIT (dict key or `events:` prefix). It is
    never inferred from the magnitude of a bare integer -- CHIME event IDs
    happen to be numerically disjoint from freq_ids today, but a selection
    grammar built on that coincidence would fail silently the day it stops
    holding.
  * Filters are exact and ANDed: a unit missing a filtered field does not
    match it. Asking for freq_ids 614,706 excludes a unit that has no freq_id
    concept at all (e.g. a per-event calibration product), and vice versa.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Optional

_ALL = ("", "all", "*")
_EVENT_PREFIXES = ("events:", "event:")
_DICT_KEYS = {"freq_ids", "events"}


def parse_freq_ids(sel) -> Optional[FrozenSet[int]]:
    """The legacy freq_id grammar, unchanged: None for 'all', else a set.

    Accepts whatever an analyzer's plan_runs hands down:
      None / 'all' / '*' / ''  -> None  (no filter -- every freq_id)
      int                      -> {int}
      list / tuple / set       -> {ints}
      '844'                    -> {844}
      '614,706'                -> {614, 706}
      '506-844'                -> {506, 507, ..., 844}
    """
    if sel is None:
        return None
    if isinstance(sel, int):
        return frozenset({sel})
    if isinstance(sel, (list, tuple, set, frozenset)):
        # empty collection == no filter, matching the legacy "empty means all"
        return frozenset(int(x) for x in sel) or None
    s = str(sel).strip().lower()
    if s in _ALL:
        return None
    out: set = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return frozenset(out) if out else None


def _parse_events(sel) -> Optional[FrozenSet[str]]:
    """Event IDs -> a frozenset of strings (None = no filter).

    Events are compared as strings because they are archive identifiers, not
    numbers: inventory rows may carry them as int or str depending on how the
    JSON was written, and a filename parse always yields str.
    """
    if sel is None:
        return None
    if isinstance(sel, (int, str)) and str(sel).strip().lower() not in _ALL:
        items = str(sel).split(",")
    elif isinstance(sel, (list, tuple, set, frozenset)):
        items = [str(x) for x in sel]
    else:
        return None
    out = frozenset(s.strip() for s in items if str(s).strip())
    return out or None


@dataclass(frozen=True)
class Selection:
    """A parsed selection: two independent, ANDed filters (None = no filter)."""
    freq_ids: Optional[FrozenSet[int]] = None
    events: Optional[FrozenSet[str]] = None

    def wants_freq_id(self, freq_id) -> bool:
        """True if `freq_id` passes the filter. A unit with NO freq_id
        (freq_id=None) fails a set filter -- exact-match semantics."""
        if self.freq_ids is None:
            return True
        if freq_id is None:
            return False
        try:
            return int(freq_id) in self.freq_ids
        except (TypeError, ValueError):
            return False

    def wants_event(self, event) -> bool:
        if self.events is None:
            return True
        if event is None:
            return False
        return str(event).strip() in self.events


def parse_selection(sel: Any) -> Selection:
    """Turn any accepted selection form into a `Selection`.

    Raises SystemExit (actionable, not a traceback) on a malformed dict or an
    empty `events:` prefix, so a typo in a plan_runs sub-selection fails loudly
    instead of silently selecting nothing.
    """
    if sel is None:
        return Selection()
    if isinstance(sel, Mapping):
        unknown = set(sel) - _DICT_KEYS
        if unknown:
            raise SystemExit(
                f"selection dict has unknown key(s) {sorted(unknown)}; "
                f"the accepted keys are {sorted(_DICT_KEYS)} "
                f"(got: {dict(sel)!r})")
        return Selection(freq_ids=parse_freq_ids(sel.get("freq_ids")),
                         events=_parse_events(sel.get("events")))
    if isinstance(sel, str):
        low = sel.strip().lower()
        for pfx in _EVENT_PREFIXES:
            if low.startswith(pfx):
                events = _parse_events(sel.strip()[len(pfx):])
                if events is None:
                    raise SystemExit(
                        f"empty event selection: {sel!r} (expected e.g. "
                        f"'events:349382977' or 'events:E1,E2')")
                return Selection(events=events)
    return Selection(freq_ids=parse_freq_ids(sel))
