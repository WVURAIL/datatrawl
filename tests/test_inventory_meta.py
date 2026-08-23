#!/usr/bin/env python3
"""
Offline test for the inventory metadata sidecar.

`survey` writes `<inventory>.meta.json` recording telescope / source / reader /
scope / freq_ids; `scan` reads it to backfill --telescope/--source/--reader so
the common case is `scan --inventory <path> --analyzer <R>`. These tests drive
that round-trip with no network and no real survey -- just the two helpers
(`write_inventory_meta`, `resolve_from_meta`) the CLI uses.

Run:  PYTHONPATH=src python tests/test_inventory_meta.py
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile

import pytest

from datatrawl import instruments as inst_mod
from datatrawl import cli_inventory
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
        meta_path = cli_inventory.write_inventory_meta(
            inv, inst, "cadc-datatrail", freq_ids="614,706",
            name="chime-ch614-706")
        assert meta_path == os.path.join(d, "inventory.meta.json")
        meta = json.load(open(meta_path))
        assert meta[cli_inventory.INVENTORY_META_SCHEMA_KEY] \
            == cli_inventory.INVENTORY_META_SCHEMA_VERSION
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
        cli_inventory.write_inventory_meta(inv, inst, "cadc-datatrail")
        args = _args(inventory=inv, root=d)
        cli_inventory.resolve_from_meta(args)
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
        cli_inventory.write_inventory_meta(inv, inst, "cadc-datatrail")
        args = _args(telescope="chime", root=d)
        cli_inventory.resolve_from_meta(args)
        assert args.source == "cadc-datatrail"
        assert args.reader == "chime-baseband"
        assert args.inventory == inv


def test_explicit_flags_win():
    """An explicit --reader/--source is never overwritten by the sidecar."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()
        cli_inventory.write_inventory_meta(inv, inst, "cadc-datatrail")
        args = _args(inventory=inv, root=d, reader="some-other-reader",
                     source="local")
        cli_inventory.resolve_from_meta(args)
        assert args.reader == "some-other-reader"   # explicit, untouched
        assert args.source == "local"               # explicit, untouched
        assert args.telescope == "chime"            # was None -> filled


def test_no_meta_is_noop():
    """An inventory with no sidecar leaves args untouched (silent no-op)."""
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()                      # inventory present, no .meta.json
        args = _args(inventory=inv, root=d)
        cli_inventory.resolve_from_meta(args)
        assert args.telescope is None
        assert args.source is None
        assert args.reader is None


@pytest.mark.parametrize("row", ["not-json", "[]", "42", "null"])
def test_meta_writer_rejects_malformed_inventory_rows(row):
    """Provenance must not silently omit scopes from corrupt inventory rows."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        with open(inv, "w") as stream:
            stream.write(row + "\n")
        with pytest.raises(ValueError, match=r"inventory\.jsonl:1:"):
            cli_inventory.write_inventory_meta(
                inv, inst, "cadc-datatrail")


@pytest.mark.parametrize("overrides", [
    {"datatrawl_inventory": 2},
    {"datatrawl_inventory": True},
    {"telescope": "../escape"},
    {"source": ["cadc-datatrail"]},
    {"reader": "bad/reader"},
])
def test_explicit_inventory_rejects_incompatible_or_unsafe_meta(overrides):
    """An explicit sidecar cannot backfill from another or unsafe schema."""
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()
        meta = {
            cli_inventory.INVENTORY_META_SCHEMA_KEY:
                cli_inventory.INVENTORY_META_SCHEMA_VERSION,
            "telescope": "chime",
            "source": "cadc-datatrail",
            "reader": "chime-baseband",
        }
        meta.update(overrides)
        with open(cli_inventory._meta_path_for(inv), "w") as stream:
            json.dump(meta, stream)

        with pytest.raises(SystemExit, match="inventory metadata|metadata"):
            cli_inventory.resolve_from_meta(_args(inventory=inv, root=d))


@pytest.mark.parametrize("payload", [
    "{",
    "[]",
    '{"telescope": "chime", "source": "cadc-datatrail"}',
])
def test_explicit_inventory_rejects_corrupt_or_unversioned_meta(payload):
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "inventory.jsonl")
        open(inv, "w").close()
        with open(cli_inventory._meta_path_for(inv), "w") as stream:
            stream.write(payload)

        with pytest.raises(SystemExit, match="inventory metadata"):
            cli_inventory.resolve_from_meta(_args(inventory=inv, root=d))


def test_sole_inventory_autofind():
    """An explicit root uses its single ``data/<name>`` inventory."""
    inst = _chime()
    saved = {k: os.environ.get(k) for k in ("HOME", "DATATRAWL_INVENTORY_ROOT")}
    with tempfile.TemporaryDirectory() as d:
        home = os.path.join(d, "home")
        os.makedirs(home)
        os.environ["HOME"] = home
        os.environ["DATATRAWL_INVENTORY_ROOT"] = os.path.join(
            home, "datatrawl-inventories")
        try:
            inv = os.path.join(d, "data", "chime", "inventory.jsonl")
            os.makedirs(os.path.dirname(inv))
            open(inv, "w").close()
            cli_inventory.write_inventory_meta(inv, inst, "cadc-datatrail")
            args = _args(root=d)
            cli_inventory.resolve_from_meta(args)
            assert args.telescope == "chime"
            assert args.inventory == inv
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_sole_inventory_autofind_uses_canonical_root_without_explicit_root():
    """Omitting --root consults DATATRAWL_INVENTORY_ROOT, not the CWD."""
    inst = _chime()
    saved = os.environ.get("DATATRAWL_INVENTORY_ROOT")
    with tempfile.TemporaryDirectory() as d:
        canonical = os.path.join(d, "inventories")
        os.environ["DATATRAWL_INVENTORY_ROOT"] = canonical
        try:
            inv = os.path.join(canonical, "chime", "inventory.jsonl")
            os.makedirs(os.path.dirname(inv))
            open(inv, "w").close()
            cli_inventory.write_inventory_meta(
                inv, inst, "cadc-datatrail")
            args = _args(root=None)

            cli_inventory.resolve_from_meta(args)

            assert args.telescope == "chime"
            assert args.inventory == inv
        finally:
            if saved is None:
                os.environ.pop("DATATRAWL_INVENTORY_ROOT", None)
            else:
                os.environ["DATATRAWL_INVENTORY_ROOT"] = saved


def test_derive_inventory_name():
    """The default name is a deterministic slug of telescope + freq_ids."""
    assert cli_inventory.derive_inventory_name("chime", None) == "chime"
    assert cli_inventory.derive_inventory_name("chime", "all") == "chime"
    assert cli_inventory.derive_inventory_name(
        "chime", "614,706") == "chime-fid614-706"
    assert cli_inventory.derive_inventory_name(
        "chime", "598") == "chime-fid598"
    assert cli_inventory.derive_inventory_name(
        "chime", "14-36") == "chime-fid14-36"
    assert cli_inventory.derive_inventory_name(
        "chime", "598,614,706") == "chime-fid598-614-706"
    # determinism: same spec -> same name (this is what keeps resume working)
    assert cli_inventory.derive_inventory_name("gbo", "614,706") \
        == cli_inventory.derive_inventory_name("gbo", "614,706")


def test_resolve_by_name():
    """`scan --name <n>` locates data/<n>/inventory.jsonl and backfills from it."""
    inst = _chime()
    with tempfile.TemporaryDirectory() as d:
        inv = os.path.join(d, "data", "chime-ch614-706", "inventory.jsonl")
        os.makedirs(os.path.dirname(inv))
        open(inv, "w").close()
        cli_inventory.write_inventory_meta(
            inv, inst, "cadc-datatrail", freq_ids="614,706",
            name="chime-ch614-706")
        args = _args(name="chime-ch614-706", root=d)
        cli_inventory.resolve_from_meta(args)
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
            fh.write(json.dumps({"scope": "test.scope", "freq_id": 614,
                                 "common_path": "cadc:TEST/x", "event": "1",
                                 "name": "baseband_1_614.h5", "size_bytes": 10,
                                 "obs_date": "2024-03-11"}) + "\n")
        cli_inventory.write_inventory_meta(
            inv, inst, "cadc-datatrail", freq_ids="614,706",
            name="chime-ch614-706")
        args = argparse.Namespace(
            source=None, name="chime-ch614-706", inventory=None,
            telescope=None, reader=None, analyzer=None,
            source_root=None, source_glob="*.h5",
            source_freq_id_regex=None, root=d)
        rc = cli.cmd_explore(args)
        assert rc == 0
        # resolve_from_meta backfilled telescope/source and pinned the resolved
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
               test_sole_inventory_autofind_uses_canonical_root_without_explicit_root,
               test_derive_inventory_name,
               test_resolve_by_name,
               test_explore_resolves_by_name):
        fn()
        print(f"PASSED: {fn.__name__}")
    print("ALL PASSED")
