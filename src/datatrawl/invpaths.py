# coding=utf-8
"""Inventory location policy: one absolute, CWD-independent home.

Inventories live under a single root so that surveys and scans agree on
where things are no matter which directory a command runs from:

    $DATATRAWL_INVENTORY_ROOT      env override
    ~/datatrawl-inventories        default

Reads resolve by name through the new root first, then the legacy
locations this project has historically written to (``./data/<name>``,
``~/data/<name>``, ``~/<name>``), printing a one-line notice when a
legacy hit is used so stragglers migrate over time. Writes always land
under the root. An explicit ``--root``/``--inventory`` still wins
everywhere -- this module only supplies the defaults.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV = "DATATRAWL_INVENTORY_ROOT"
DEFAULT_ROOT = "~/datatrawl-inventories"


def inventory_root() -> Path:
    """The canonical inventory root (env override, else the default)."""
    return Path(os.environ.get(ENV, DEFAULT_ROOT)).expanduser()


def inventory_dir_for_write(name: str) -> Path:
    """Where a new inventory named ``name`` is written."""
    return inventory_root() / str(name).strip()


def legacy_candidates(name: str, cwd=None):
    """Legacy read locations, most-recent convention first."""
    base = Path(cwd) if cwd else Path(os.getcwd())
    n = str(name).strip()
    return [base / "data" / n, Path.home() / "data" / n, Path.home() / n]


def resolve_inventory(name: str, cwd=None) -> Path:
    """Resolve ``<dir>/inventory.jsonl`` for reading, by name.

    Checks the canonical root, then the legacy locations; returns the
    canonical path if nothing exists yet (callers produce their own
    not-found error against the canonical location).
    """
    canonical = inventory_dir_for_write(name) / "inventory.jsonl"
    if canonical.exists():
        return canonical
    for d in legacy_candidates(name, cwd=cwd):
        p = d / "inventory.jsonl"
        if p.exists():
            print(f"[datatrawl] inventory '{name}' found at legacy path {p}; "
                  f"consider: mv {d} {inventory_dir_for_write(name)}",
                  flush=True)
            return p
    return canonical
