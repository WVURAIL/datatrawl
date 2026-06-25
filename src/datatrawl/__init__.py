"""
datatrawl -- storage-safe, pluggable, resumable analysis of archived telescope
data on the CANFAR Science Platform.

Discover what's available with the `doctor` / `list` commands, then run a
survey + scan for your (telescope, source, reader, analyzer) combination.
"""
from __future__ import annotations

from .analyzer_base import AccumulatingAnalyzer

__version__ = "0.1.0"
__all__ = ["AccumulatingAnalyzer"]
