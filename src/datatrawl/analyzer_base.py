"""
A base class for analyzers that accumulate a product over a stream of arrays and
checkpoint it to a `.npz`.

Almost every analysis has the same shape: allocate accumulators, fold in each
file's arrays in one streaming pass, and write the result so an interrupted run
can resume. The mechanical parts of that -- a crash-safe atomic write, reloading
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
for the crash-safe write.
"""
from __future__ import annotations

import datetime
import os
import tempfile
from typing import Any, Mapping

import numpy as np

from .interfaces import Analyzer, RunContext


class AccumulatingAnalyzer(Analyzer):
    def __init__(self) -> None:
        self._keys: list[str] = []     # Unit keys already in the product (resume/dedup)
        self._names: list[str] = []    # human-readable file names, for provenance

    # -- provenance / dedup --------------------------------------------------
    def _record(self, meta: Mapping[str, Any]) -> None:
        """Call once per file in consume_file() to log it for resume + dedup."""
        key = str(meta.get("unit_key", meta.get("unit_name", "?")))
        self._keys.append(key)
        self._names.append(str(meta.get("unit_name", key)))

    def processed_keys(self) -> set:
        return set(self._keys)

    # -- crash-safe write (reuse this even when you override save) ------------
    @staticmethod
    def _atomic_savez(path: str, **arrays: Any) -> None:
        """Write a `.npz` via a temp file + atomic rename, so a kill mid-write
        never leaves a half-written product behind."""
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

    def save(self, path: str) -> None:
        # Our provenance keys win, so a product dict can't clobber them.
        fields = {
            **dict(self._product()),
            "files": np.array(self._names),
            "unit_keys": np.array(self._keys),
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self._atomic_savez(path, **fields)

    def resume(self, path: str, ctx: RunContext) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        self._restore(z)
        self._keys = [str(x) for x in z["unit_keys"]]
        self._names = [str(x) for x in z["files"]]
        return True
