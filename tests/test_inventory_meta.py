#!/usr/bin/env python3
"""
Offline test for the inventory metadata sidecar.

`survey` writes `<inventory>.meta.json` recording telescope / source / reader /
scope / freq_ids; `scan` reads it to backfill --telescope/--source/--reader so
the common case is `scan --inventory <path> --analyzer <R>`. These tests drive
that round-trip with no network and no real survey -- just the two helpers
(`write_inventory_meta`, `_resolve_from_meta`) the CLI uses.

Run:  PYTHONPATH=src python tests/test_inventory_meta.py
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile

from datatrawl import instruments as inst_mod
import datatrawl.cli as cli


def _chime():
    return inst_mod.load_instrument("chime")


def _args(**kw):
    base = dict(telescope=None, source=None, reader=None, analyzer="spectrum",
                inventory=None, name=None, root=os.getcwd())
    base.update(kw)
    return argparse.Namespace(**base)


def test_meta_roundtrip():
    """survey's writer stamps the expected fields next to the inventory."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()
        meta_path = cli.write_inventory_meta(inv, inst, "cadc-datatrail",
                                             freq_ids="614,706", name="chime-ch614-706")
        assert meta_path == os.path.join(d, "inventory.meta.json")
        meta = json.load(open(meta_path))
        assert meta["datatrawl_inventory"] == 1
        assert meta["name"] == "chime-ch614-706"
        assert meta["telescope"] == "chime"
        assert meta["source"] == "cadc-datatrail"
        assert meta["reader"] == "chime-baseband"
        # empty inventory + no --scope -> meta falls back to the telescope's declared
        # baseband scopes (chime registers both event and scheduled).
        assert meta["scope"] == "chime.event.baseband.raw,chime.scheduled.baseband.raw"
        assert meta["scopes"] == ["chime.event.baseband.raw",
                                  "chime.scheduled.baseband.raw"]
        assert meta["freq_ids"] == "614,706"
        assert meta.get("created")


def test_backfill_from_inventory_flag():
    """`scan --inventory X` fills telescope/source/reader from the sidecar."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()
        cli.write_inventory_meta(inv, inst, "cadc-datatrail")
        args = _args(inventory=inv, root=d)
        cli._resolve_from_meta(args)
        assert args.telescope == "chime"
        assert args.source == "cadc-datatrail"
        assert args.reader == "chime-baseband"


def test_backfill_from_telescope_default_path():
    """`scan --telescope chime` (no --inventory) finds data/chime/inventory.jsonl."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "data", "chime", "inventory.jsonl")
        os.makedirs(os.path.dirname(inv))
        open(inv, "w").close()
        cli.write_inventory_meta(inv, inst, "cadc-datatrail")
        args = _args(telescope="chime", root=d)
        cli._resolve_from_meta(args)
        assert args.source == "cadc-datatrail"
        assert args.reader == "chime-baseband"
        assert args.inventory == inv


def test_explicit_flags_win():
    """An explicit --reader/--source is never overwritten by the sidecar."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()
        cli.write_inventory_meta(inv, inst, "cadc-datatrail")
        args = _args(inventory=inv, root=d, reader="some-other-reader",
                     source="local")
        cli._resolve_from_meta(args)
        assert args.reader == "some-other-reader"   # explicit, untouched
        assert args.source == "local"               # explicit, untouched
        assert args.telescope == "chime"            # was None -> filled


def test_no_meta_is_noop():
    """An inventory with no sidecar leaves args untouched (silent no-op)."""
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()                      # inventory present, no .meta.json
        args = _args(inventory=inv, root=d)
        cli._resolve_from_meta(args)
        assert args.telescope is None
        assert args.source is None
        assert args.reader is None


def test_sole_inventory_autofind():
    """With neither --inventory nor --telescope, the single inventory is used."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "data", "chime", "inventory.jsonl")
        os.makedirs(os.path.dirname(inv))
        open(inv, "w").close()
        cli.write_inventory_meta(inv, inst, "cadc-datatrail")
        args = _args(root=d)
        cli._resolve_from_meta(args)
        assert args.telescope == "chime"
        assert args.inventory == inv


def test_derive_inventory_name():
    """The default name is a deterministic slug of telescope + freq_ids."""
    assert cli.derive_inventory_name("chime", None) == "chime"
    assert cli.derive_inventory_name("chime", "all") == "chime"
    assert cli.derive_inventory_name("chime", "614,706") == "chime-fid614-706"
    assert cli.derive_inventory_name("chime", "598") == "chime-fid598"
    assert cli.derive_inventory_name("chime", "14-36") == "chime-fid14-36"
    assert cli.derive_inventory_name("chime", "598,614,706") == "chime-fid598-614-706"
    # determinism: same spec -> same name (this is what keeps resume working)
    assert cli.derive_inventory_name("gbo", "614,706") \
        == cli.derive_inventory_name("gbo", "614,706")


def test_resolve_by_name():
    """`scan --name <n>` locates data/<n>/inventory.jsonl and backfills from it."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "data", "chime-ch614-706", "inventory.jsonl")
        os.makedirs(os.path.dirname(inv))
        open(inv, "w").close()
        cli.write_inventory_meta(inv, inst, "cadc-datatrail", freq_ids="614,706",
                                 name="chime-ch614-706")
        args = _args(name="chime-ch614-706", root=d)
        cli._resolve_from_meta(args)
        assert args.inventory == inv
        assert args.telescope == "chime"
        assert args.reader == "chime-baseband"


def test_explore_resolves_by_name():
    """`explore --name <n>` finds data/<n>/inventory.jsonl through the meta
    sidecar (not the telescope-default dir) and enumerates it -- the same
    resolution `scan` uses, so the README's Step 5/Step 6 `--name` flow works."""
    from datatrawl import registry
    registry.load_plugins()          # cmd_explore instantiates the source; the
                                     # real CLI loads plugins in main() before dispatch
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "data", "chime-ch614-706", "inventory.jsonl")
        os.makedirs(os.path.dirname(inv))
        with open(inv, "w") as fh:
            fh.write(json.dumps({"freq_id": 614, "common_path": "cadc:TEST/x",
                                 "event": "1", "size_bytes": 10,
                                 "obs_date": "2024-03-11"}) + "\n")
        cli.write_inventory_meta(inv, inst, "cadc-datatrail",
                                 freq_ids="614,706", name="chime-ch614-706")
        args = argparse.Namespace(
            source=None, name="chime-ch614-706", inventory=None,
            telescope=None, reader=None, analyzer=None,
            source_root=None, source_glob="*.h5",
            source_freq_id_regex=None, root=d)
        rc = cli.cmd_explore(args)
        assert rc == 0
        # _resolve_from_meta backfilled telescope/source and pinned the resolved
        # path, rather than falling back to data/chime/inventory.jsonl. --source
        # is optional for explore when --name resolves it (mirrors `scan`).
        assert args.telescope == "chime"
        assert args.source == "cadc-datatrail"
        assert args.inventory == inv


if __name__ == "__main__":
    for fn in (test_meta_roundtrip,
               test_backfill_from_inventory_flag,
               test_backfill_from_telescope_default_path,
               test_explicit_flags_win,
               test_no_meta_is_noop,
               test_sole_inventory_autofind,
               test_derive_inventory_name,
               test_resolve_by_name,
               test_explore_resolves_by_name):
        fn()
        print(f"PASSED: {fn.__name__}")
    print("ALL PASSED")
