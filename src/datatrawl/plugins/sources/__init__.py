"""Source plugins -- and the Datatrail adapter, re-exported.

The adapter (`DATATRAIL` / `Datatrail`) is the ONE sanctioned surface for
talking to datatrail from analyzers and user code (e.g. `files()` for
lazily resolving a per-day companion dataset). Import it from here, not
from the underscored module.
"""
from ._datatrail import DATATRAIL, Datatrail

__all__ = ["DATATRAIL", "Datatrail"]
