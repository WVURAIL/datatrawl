"""
datatrawl -- storage-safe, pluggable, resumable analysis of archived telescope
data on the CANFAR Science Platform.

Discover what's available with the `doctor` / `list` commands, then run a
survey + scan for your (telescope, source, reader, analyzer) combination.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .analyzer_base import AccumulatingAnalyzer

try:
    __version__ = version("datatrawl")
except PackageNotFoundError:  # source tree on PYTHONPATH, not installed
    __version__ = "0+unknown"

__all__ = ["AccumulatingAnalyzer", "__version__"]
