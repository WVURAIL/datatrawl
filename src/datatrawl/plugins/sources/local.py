r"""
Local-directory data source.

For data already sitting on /arc (or anywhere on the filesystem) rather than in a
remote archive. `enumerate` globs files for the selection; `fetch` hardlinks (or
copies) the file into the engine's scratch dir -- so the engine's "delete after
reduce" only ever removes the scratch link, never your original.

Two real uses:
  * collaborators whose data is staged on /arc, not pulled from CADC;
  * the test/demo path, where a synthetic library stands in for an archive.

Selection (`ctx.selection`) may be:
  * a list of ints  -> files whose freq_id (parsed as an exact integer from the
    filename via `--source-freq-id-regex`, default `_(\d+)\.h5$`) is in the list,
    so selecting 44 does not match `..._844.h5`;
  * None / "all"    -> every file matching `--source-glob`.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
from typing import Iterable, List

from ...interfaces import DataSource, RunContext, Unit, PluginInfo, READY
from ...registry import source as _register_source


@_register_source
class LocalDirectorySource(DataSource):
    info = PluginInfo(
        name="local",
        kind="source",
        summary="Files already on disk (/arc or elsewhere); no archive access.",
        status=READY,
        instruments=("*",),
        requires=("a directory of input files",),
        notes="Stages by hardlink/copy into scratch, so your originals are never "
              "deleted. Set --source-root and optionally --source-glob.",
    )

    def enumerate(self, ctx: RunContext) -> Iterable[Unit]:
        o = ctx.options or {}
        root = o.get("source_root")
        if not root:
            raise SystemExit("local source: pass --source-root <dir>")
        pattern = o.get("source_glob", "*.h5")
        paths = sorted(glob.glob(os.path.join(root, "**", pattern), recursive=True))
        sel = ctx.selection
        wanted = None
        if isinstance(sel, (list, tuple)) and sel and all(
                isinstance(x, int) for x in sel):
            wanted = {int(x) for x in sel}
        # Parse the freq_id as an exact integer from the filename (so selecting 44
        # does not match baseband_..._844.h5). Override the capture for other
        # naming schemes with --source-freq-id-regex.
        # `or` (not a .get default) so an explicit None/"" from a caller -- e.g.
        # `explore`, which always populates this key -- still falls back instead
        # of reaching re.compile(None).
        freq_id_re = re.compile(o.get("source_freq_id_regex") or r"_(\d+)\.h5$")
        units: List[Unit] = []
        for p in paths:
            src_path = os.path.abspath(p)
            name = os.path.basename(p)
            match = freq_id_re.search(name)
            freq_id = int(match.group(1)) if match else None
            if wanted is not None and freq_id not in wanted:
                continue
            meta: dict[str, object] = {"src_path": src_path}
            if freq_id is not None:
                # `explore` reads this metadata to summarize the available bands.
                meta["freq_id"] = freq_id
            units.append(Unit(key=src_path, name=name, meta=meta))
        return units

    def fetch(self, unit: Unit, dest: str) -> tuple[bool, str]:
        src = unit.meta.get("src_path", unit.key)
        try:
            if os.path.exists(dest):
                os.remove(dest)
            try:
                os.link(src, dest)                 # cheap: hardlink if same fs
            except OSError:
                shutil.copy2(src, dest)            # fallback: copy across fs
            return (os.path.getsize(dest) > 0), ""
        except Exception as exc:                   # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def preflight(self, ctx: RunContext) -> tuple[bool, list[str]]:
        root = (ctx.options or {}).get("source_root")
        if not root:
            return False, ["--source-root not set"]
        if not os.path.isdir(root):
            return False, [f"--source-root is not a directory: {root}"]
        return True, []
