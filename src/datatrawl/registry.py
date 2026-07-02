"""
datatrawl.registry -- the plugin table behind `list` and `doctor`.

Sources, readers, and analyzers register themselves at import time via the
`@source` / `@reader` / `@analyzer` decorators. The CLI imports the `plugins`
package once (which imports every plugin module), after which `available(kind)`
returns the registered classes and `describe(kind)` returns their PluginInfo
rows for the discovery tables.

Instruments are NOT registered here -- they are YAML files discovered on disk by
instruments.py -- but `list_instruments()` is re-exported so the CLI has a single
place to ask "what can I pick from?".
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re
import sys
from typing import Dict, Iterable, List, Type

from .interfaces import DataSource, Reader, Analyzer, PluginInfo

_SOURCES: Dict[str, Type[DataSource]] = {}
_READERS: Dict[str, Type[Reader]] = {}
_ANALYZERS: Dict[str, Type[Analyzer]] = {}

_TABLES = {"source": _SOURCES, "reader": _READERS, "analyzer": _ANALYZERS}


def _register(table: Dict[str, type], cls: type) -> type:
    info: PluginInfo = getattr(cls, "info", None)
    if info is None:
        raise TypeError(f"{cls.__name__} must define a class attribute "
                        f"`info = PluginInfo(...)` to be registered")
    if info.name in table:
        raise ValueError(f"duplicate {info.kind} plugin name: {info.name!r}")
    table[info.name] = cls
    return cls


def source(cls: Type[DataSource]) -> Type[DataSource]:
    return _register(_SOURCES, cls)


def reader(cls: Type[Reader]) -> Type[Reader]:
    return _register(_READERS, cls)


def analyzer(cls: Type[Analyzer]) -> Type[Analyzer]:
    return _register(_ANALYZERS, cls)


# --------------------------------------------------------------------------
# Lookup / discovery
# --------------------------------------------------------------------------
def _table(kind: str) -> Dict[str, type]:
    try:
        return _TABLES[kind]
    except KeyError:
        raise KeyError(f"unknown plugin kind {kind!r}; "
                       f"expected one of {sorted(_TABLES)}")


def available(kind: str) -> Dict[str, type]:
    return dict(_table(kind))


def get(kind: str, name: str) -> type:
    table = _table(kind)
    if name not in table:
        opts = ", ".join(sorted(table)) or "(none registered)"
        raise KeyError(f"no {kind} named {name!r}. Available: {opts}")
    return table[name]


def describe(kind: str) -> List[PluginInfo]:
    """PluginInfo rows for one kind, sorted ready-first then by name."""
    infos = [cls.info for cls in _table(kind).values()]
    return sorted(infos, key=lambda i: (i.status_rank, i.name))


_BUILTINS_LOADED = False
_LOADED_TARGETS: set = set()
_EP_LOADED = False


def _import_target(target: str) -> None:
    """Import one plugin target so its @source/@reader/@analyzer decorators run.

    `target` is either a dotted module path ('mypkg.analyzers.fstat', importable
    on PYTHONPATH or pip-installed) or a path to a .py file ('/arc/proj/fstat.py').
    File paths are handy for loose science code that isn't a pip package.
    """
    t = target.strip()
    if not t or t in _LOADED_TARGETS:
        return
    is_path = t.endswith(".py") or os.sep in t or (os.altsep and os.altsep in t)
    if is_path:
        path = os.path.abspath(os.path.expanduser(t))
        if not os.path.exists(path):
            raise SystemExit(f"--plugin file not found: {path}")
        modname = "datatrawl_ext_" + re.sub(r"\W", "_", path)
        if modname not in sys.modules:
            spec = importlib.util.spec_from_file_location(modname, path)
            if spec is None or spec.loader is None:
                raise SystemExit(f"could not load plugin file: {path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as exc:                       # noqa: BLE001
                sys.modules.pop(modname, None)             # no half-loaded module left behind
                hint = ""
                if isinstance(exc, ImportError) and "relative import" in str(exc):
                    hint = ("\n  This file uses a relative import, which only works "
                            "inside an importable package -- not when a single file is "
                            "loaded by path. Make the package importable (pip install "
                            "it, or put its root on PYTHONPATH) and load it by module "
                            "name (--plugin yourpkg.module) or via its 'datatrawl.plugins' "
                            "entry point instead.")
                raise SystemExit(f"--plugin {t}: failed to import "
                                 f"({type(exc).__name__}: {exc}){hint}")
    else:
        try:
            importlib.import_module(t)
        except Exception as exc:                           # noqa: BLE001
            raise SystemExit(f"--plugin {t}: could not import module "
                             f"({type(exc).__name__}: {exc}). Check it is installed "
                             f"or on PYTHONPATH and spelled as a dotted module path.")
    _LOADED_TARGETS.add(t)


def _load_entry_point_plugins() -> None:
    """Import plugins advertised by installed packages under group 'datatrawl.plugins'.

    A plugin package declares, in its own pyproject.toml:

        [project.entry-points."datatrawl.plugins"]
        fstat = "mypkg.analyzers.fstat"
    """
    global _EP_LOADED
    if _EP_LOADED:
        return
    _EP_LOADED = True
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = (eps.select(group="datatrawl.plugins")
                 if hasattr(eps, "select") else eps.get("datatrawl.plugins", []))
    except Exception:                                    # no metadata available
        return
    for ep in group:
        try:
            ep.load()                                    # imports module -> registers
        except Exception as exc:                         # one bad plugin must not
            import warnings                              # break the whole tool
            warnings.warn(
                f"datatrawl: entry-point plugin {ep.name!r} failed to "
                f"load: {exc}. If the providing package was updated since "
                f"it was installed, its entry-point metadata may be stale; "
                f"re-run `pip install -e <its repo>` to refresh it."
            )


def load_plugins(extra: Iterable[str] = ()) -> None:
    """Register all plugins so `list`/`doctor`/`get` can see them.

    Always loads the built-ins; then, additively:
      * entry-point plugins (installed packages, group 'datatrawl.plugins'),
      * targets in the DATATRAWL_PLUGINS env var (os.pathsep- or comma-separated),
      * anything in `extra` (e.g. the CLI's --plugin).
    Each target is a dotted module or a path to a .py file. Idempotent: a target
    already imported is skipped, so this is safe to call more than once.
    """
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        from . import plugins  # noqa: F401  (side effect: registers the built-ins)
        _BUILTINS_LOADED = True
    _load_entry_point_plugins()
    env = os.environ.get("DATATRAWL_PLUGINS", "")
    targets = [t for t in re.split(r"[%s,]" % re.escape(os.pathsep), env) if t.strip()]
    targets += list(extra or ())
    for t in targets:
        _import_target(t)
