import os
from pathlib import Path

from datatrawl import invpaths


def _mk(dirpath):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "inventory.jsonl").write_text("{}\n")


def test_default_root_is_home_and_env_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv(invpaths.ENV, raising=False)
    assert invpaths.inventory_root() == Path.home() / "datatrawl-inventories"
    monkeypatch.setenv(invpaths.ENV, str(tmp_path / "inv"))
    assert invpaths.inventory_root() == tmp_path / "inv"


def test_resolution_order(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(invpaths.ENV, str(home / "datatrawl-inventories"))
    # nothing exists: canonical path returned
    p = invpaths.resolve_inventory("x", cwd=cwd)
    assert p == home / "datatrawl-inventories" / "x" / "inventory.jsonl"
    # oldest legacy only
    _mk(home / "x")
    assert invpaths.resolve_inventory("x", cwd=cwd) == home / "x" / "inventory.jsonl"
    # ~/data beats ~/<name>
    _mk(home / "data" / "x")
    assert (invpaths.resolve_inventory("x", cwd=cwd)
            == home / "data" / "x" / "inventory.jsonl")
    # ./data beats both
    _mk(cwd / "data" / "x")
    assert (invpaths.resolve_inventory("x", cwd=cwd)
            == cwd / "data" / "x" / "inventory.jsonl")
    # canonical beats every legacy location
    _mk(home / "datatrawl-inventories" / "x")
    assert (invpaths.resolve_inventory("x", cwd=cwd)
            == home / "datatrawl-inventories" / "x" / "inventory.jsonl")


def test_write_dir_under_root(monkeypatch, tmp_path):
    monkeypatch.setenv(invpaths.ENV, str(tmp_path))
    assert invpaths.inventory_dir_for_write("abc") == tmp_path / "abc"
