"""
A base class for analyzers that accumulate a product over a stream of arrays and
checkpoint it to a `.npz`.

Almost every analysis has the same shape: allocate accumulators, fold in each
file's arrays in one streaming pass, and write the result so an interrupted run
can resume. The mechanical parts of that -- atomic replacement, reloading
on resume, and tracking which files are already in the product -- are identical
everywhere, so they live here. Subclass and write only the science:

    __init__()                    allocate neutral accumulator state
    begin(ctx, first_meta)        capture first-file/run metadata without
                                  overwriting restored accumulator state
    consume_file(arrays, meta)    fold in one file; call self._record(meta)
    _product()                    return fields to persist
    _restore(z)                   restore accumulators from a loaded product

`save()`, `resume()`, and `processed_keys()` then come for free. An analysis that
needs custom resume validation or derived fields at save time (see the `spectrum`
analyzer) overrides `save()`/`resume()` directly and reuses `self._atomic_savez()`
for the same replacement behavior.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from collections.abc import Mapping as MappingABC
from typing import Any, Mapping, Optional

import numpy as np

from .interfaces import Analyzer, RunContext


class AccumulatingAnalyzer(Analyzer):
    _MANIFEST_SCHEMA = "datatrawl.accumulating/v3"
    # Subclasses should override this with a stable, versioned algorithm/product
    # identity and bump it whenever persisted fields or scientific semantics
    # change. The class-qualified fallback keeps existing external analyzers
    # usable while still giving every product a fail-closed v1 identity.
    _PRODUCT_SCHEMA: Optional[str] = None
    _MAX_REPORTED_MANIFEST_DIFFERENCES = 8

    def __init__(self) -> None:
        self._keys: list[str] = []     # Unit keys already committed (resume skip)
        self._names: list[str] = []    # human-readable file names, for provenance
        self._resume_manifest_json: Optional[str] = None

    # -- provenance / resume bookkeeping ------------------------------------
    def _record(self, meta: Mapping[str, Any]) -> None:
        """Call once per file in consume_file() to log it for resume skipping."""
        try:
            key = meta["unit_key"]
            name = meta["unit_name"]
        except KeyError as exc:
            raise ValueError(
                f"analyzer metadata is missing required {exc.args[0]!r}; "
                "consume files through the datatrawl engine"
            ) from exc
        if not isinstance(key, str) or not key:
            raise ValueError("analyzer metadata unit_key must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ValueError("analyzer metadata unit_name must be a non-empty string")
        self._keys.append(key)
        self._names.append(name)

    def processed_keys(self) -> set:
        return set(self._keys)

    # -- atomic replacement (reuse this even when you override save) ----------
    @staticmethod
    def _atomic_savez(path: str, **arrays: Any) -> None:
        """Write a `.npz` via a temp file + atomic rename.

        For an ordinary process interruption, the prior checkpoint remains in
        place until replacement. This does not claim power-loss durability.
        """
        d = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(suffix=".npz", dir=d)
        os.close(fd)
        try:
            np.savez_compressed(tmp, **arrays)
            os.replace(tmp, path)                          # atomic
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # -- default save / resume (override for custom validation) --------------
    def _product(self) -> Mapping[str, Any]:
        """Return the {name: array} to persist. Implement for the default save()."""
        raise NotImplementedError

    def _restore(self, z: Mapping[str, Any]) -> None:
        """Repopulate accumulators from a loaded `.npz`. Implement for resume()."""
        raise NotImplementedError

    def resume_parameters(self, ctx: RunContext) -> Mapping[str, Any]:
        """Meaning-changing analyzer options included in the resume fingerprint.

        The fail-closed default includes every ``ctx.options`` entry. An analyzer
        may override this to select only the options that affect its product, but
        must never omit a parameter whose change would make two runs
        scientifically incompatible.
        """
        return dict(ctx.options or {})

    @classmethod
    def product_schema(cls) -> str:
        """Stable versioned identity for this analyzer's persisted semantics."""
        schema = cls._PRODUCT_SCHEMA
        if schema is None:
            schema = f"{cls.__module__}.{cls.__qualname__}/v1"
        if (not isinstance(schema, str) or not schema.strip()
                or "/v" not in schema
                or not schema.rsplit("/v", 1)[1].isdigit()
                or int(schema.rsplit("/v", 1)[1]) < 1):
            raise TypeError(
                f"{cls.__module__}.{cls.__qualname__}._PRODUCT_SCHEMA must "
                "be a non-empty versioned string ending in /vN (N >= 1)")
        return schema

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        """Convert a run manifest value to deterministic, JSON-compatible data."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        if isinstance(value, MappingABC):
            return {str(k): cls._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._jsonable(v) for v in value]
        if isinstance(value, (set, frozenset)):
            converted = [cls._jsonable(v) for v in value]
            return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
        if hasattr(value, "item"):
            try:
                return cls._jsonable(value.item())
            except (TypeError, ValueError):
                pass
        if hasattr(value, "__dict__"):
            return {
                str(k): cls._jsonable(v)
                for k, v in vars(value).items()
                if not str(k).startswith("_") and not callable(v)
            }
        raise TypeError(
            f"resume manifest value {value!r} ({type(value).__name__}) is not "
            "deterministically serializable; override resume_parameters() to "
            "return JSON-compatible values")

    def _resume_manifest(self, ctx: RunContext) -> dict[str, Any]:
        info = getattr(self, "info", None)
        instrument = getattr(ctx, "instrument", None)
        return {
            "schema": self._MANIFEST_SCHEMA,
            "analyzer": {
                "name": getattr(info, "name", type(self).__name__),
                "class": f"{type(self).__module__}.{type(self).__qualname__}",
                "product_schema": self.product_schema(),
            },
            "instrument": self._jsonable(instrument),
            "selection": self._jsonable(ctx.selection),
            "parameters": self._jsonable(self.resume_parameters(ctx)),
        }

    def _prepare_resume_manifest(self, ctx: RunContext) -> str:
        manifest = self._resume_manifest(ctx)
        return json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)

    @classmethod
    def _manifest_differences(cls, recorded: Any, expected: Any,
                              path: str = "") -> list[str]:
        """Return concise, field-addressed differences for resume diagnostics."""
        if isinstance(recorded, MappingABC) and isinstance(expected, MappingABC):
            out: list[str] = []
            for key in sorted(set(recorded) | set(expected), key=str):
                field = f"{path}.{key}" if path else str(key)
                if key not in recorded:
                    out.append(f"{field}: missing from existing product")
                elif key not in expected:
                    out.append(f"{field}: only in existing product")
                else:
                    out.extend(cls._manifest_differences(
                        recorded[key], expected[key], field))
            return out
        if recorded != expected:
            old = json.dumps(recorded, sort_keys=True, ensure_ascii=True)
            new = json.dumps(expected, sort_keys=True, ensure_ascii=True)
            return [f"{path or '<manifest>'}: existing={old}, requested={new}"]
        return []

    def save(self, path: str) -> None:
        if self._resume_manifest_json is None:
            raise RuntimeError(
                "resume manifest was not prepared; the engine must call "
                "resume(path, ctx) before save()")
        # Our provenance keys win, so a product dict can't clobber them.
        fields = {
            **dict(self._product()),
            "_datatrawl_manifest": np.array(self._resume_manifest_json),
            "_datatrawl_product_schema": np.array(self.product_schema()),
            "files": np.array(self._names),
            "unit_keys": np.array(self._keys),
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self._atomic_savez(path, **fields)

    def resume(self, path: str, ctx: RunContext) -> bool:
        expected = self._prepare_resume_manifest(ctx)
        self._resume_manifest_json = expected
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        try:
            if "_datatrawl_product_schema" not in z.files:
                raise SystemExit(
                    f"error: {path} predates explicit product-schema identity. "
                    "It cannot be continued without risking mixed algorithm "
                    "semantics; use a fresh output path.")
            recorded_schema = str(z["_datatrawl_product_schema"])
            expected_schema = self.product_schema()
            if recorded_schema != expected_schema:
                raise SystemExit(
                    f"error: {path} has product schema "
                    f"{recorded_schema!r}, but this analyzer requires "
                    f"{expected_schema!r}. Use a fresh output path.")
            if "_datatrawl_manifest" not in z.files:
                raise SystemExit(
                    f"error: {path} predates the safe resume manifest. It cannot "
                    "be continued without risking mixed run parameters; use a "
                    "fresh output path.")
            recorded = str(z["_datatrawl_manifest"])
            try:
                recorded_manifest = json.loads(recorded)
            except (TypeError, ValueError) as exc:
                raise SystemExit(
                    f"error: {path} has an invalid safe-resume manifest; use "
                    "a fresh output path.") from exc
            expected_manifest = json.loads(expected)
            if recorded_manifest != expected_manifest:
                differences = self._manifest_differences(
                    recorded_manifest, expected_manifest)
                limit = self._MAX_REPORTED_MANIFEST_DIFFERENCES
                detail = "; ".join(differences[:limit])
                if len(differences) > limit:
                    detail += (
                        f"; and {len(differences) - limit} more difference(s)")
                raise SystemExit(
                    f"error: {path} was built for a different analyzer, "
                    "instrument, selection, or run parameter set. "
                    f"Differences: {detail}. Use a fresh output path.")
            self._restore(z)
            self._keys = [str(x) for x in z["unit_keys"]]
            self._names = [str(x) for x in z["files"]]
            return True
        finally:
            z.close()
