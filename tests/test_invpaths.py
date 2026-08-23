from pathlib import Path

import pytest

from datatrawl import invpaths


def test_default_root_is_home_and_env_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv(invpaths.ENV, raising=False)
    assert invpaths.inventory_root() == Path.home() / "datatrawl-inventories"
    monkeypatch.setenv(invpaths.ENV, str(tmp_path / "inv"))
    assert invpaths.inventory_root() == tmp_path / "inv"


def test_resolution_is_canonical_and_cwd_independent(monkeypatch, tmp_path):
    canonical = tmp_path / "inventories"
    monkeypatch.setenv(invpaths.ENV, str(canonical))
    assert invpaths.resolve_inventory("x") == canonical / "x" / "inventory.jsonl"


def test_write_dir_under_root(monkeypatch, tmp_path):
    monkeypatch.setenv(invpaths.ENV, str(tmp_path))
    assert invpaths.inventory_dir_for_write("abc") == tmp_path / "abc"


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "/absolute"])
def test_managed_inventory_names_cannot_escape_root(monkeypatch, tmp_path, name):
    monkeypatch.setenv(invpaths.ENV, str(tmp_path))
    with pytest.raises(ValueError, match="invalid inventory name"):
        invpaths.inventory_dir_for_write(name)


def test_inventory_root_env_must_be_absolute(monkeypatch):
    monkeypatch.setenv(invpaths.ENV, "relative/inventories")
    with pytest.raises(ValueError, match="must be an absolute path"):
        invpaths.inventory_root()
