"""
Importing this package imports every plugin module, which runs the
@source/@reader/@analyzer decorators and populates the registry. The CLI calls
registry.load_plugins() (which imports this) exactly once at startup.

To add a plugin, drop a module in sources/ readers/ or analyzers/ and add it to
the matching import list below -- or keep it in your own project and load it with
`--plugin` / DATATRAWL_PLUGINS / an entry point (see docs/ADDING_AN_ANALYZER.md).
The lists are explicit rather than auto-globbed so a broken plugin fails loudly
with a clear traceback instead of being silently skipped.
"""
from __future__ import annotations

# sources
from .sources import local            # noqa: F401
from .sources import cadc_datatrail   # noqa: F401

# readers
from .readers import chime_baseband   # noqa: F401

# analyzers
from .analyzers import spectrum        # noqa: F401  (the worked example)
