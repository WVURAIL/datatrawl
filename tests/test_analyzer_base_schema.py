"""Fail-closed product-schema identity for AccumulatingAnalyzer products."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from datatrawl.analyzer_base import AccumulatingAnalyzer
from datatrawl.interfaces import RunContext


class _SchemaAnalyzer(AccumulatingAnalyzer):
    _PRODUCT_SCHEMA = "tests.schema-analyzer/v1"

    def _product(self):
        return {"value": np.array(1)}

    def _restore(self, z):
        self.value = int(z["value"])


def _ctx():
    return RunContext(
        instrument=SimpleNamespace(name="test", fs_hz=1.0),
        selection=[1],
        options={},
    )


def _write_product(path):
    analyzer = _SchemaAnalyzer()
    assert analyzer.resume(str(path), _ctx()) is False
    analyzer.save(str(path))


def test_base_product_stamps_schema_in_field_and_manifest(tmp_path):
    product = tmp_path / "product.npz"
    _write_product(product)

    with np.load(product, allow_pickle=False) as saved:
        assert str(saved["_datatrawl_product_schema"]) \
            == "tests.schema-analyzer/v1"
        manifest = json.loads(str(saved["_datatrawl_manifest"]))
        assert manifest["schema"] == "datatrawl.accumulating/v3"
        assert manifest["analyzer"]["product_schema"] \
            == "tests.schema-analyzer/v1"


def test_base_resume_refuses_changed_algorithm_schema(tmp_path, monkeypatch):
    product = tmp_path / "product.npz"
    _write_product(product)
    monkeypatch.setattr(
        _SchemaAnalyzer, "_PRODUCT_SCHEMA", "tests.schema-analyzer/v2")

    with pytest.raises(SystemExit, match="product schema"):
        _SchemaAnalyzer().resume(str(product), _ctx())


def test_base_resume_refuses_product_without_schema_identity(tmp_path):
    product = tmp_path / "product.npz"
    _write_product(product)
    with np.load(product, allow_pickle=False) as saved:
        fields = {
            name: np.array(saved[name], copy=True)
            for name in saved.files
            if name != "_datatrawl_product_schema"
        }
    np.savez(product, **fields)

    with pytest.raises(SystemExit, match="predates explicit product-schema"):
        _SchemaAnalyzer().resume(str(product), _ctx())
