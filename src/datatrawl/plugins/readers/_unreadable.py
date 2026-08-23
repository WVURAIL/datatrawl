"""Translate expected on-disk failures into an explicit data disposition."""
from __future__ import annotations

from contextlib import contextmanager

from ...interfaces import UnreadableUnitError


@contextmanager
def unreadable_file():
    """Mark HDF5/open/schema failures without hiding programming errors."""
    try:
        yield
    except UnreadableUnitError:
        raise
    except (OSError, KeyError, ValueError) as exc:
        raise UnreadableUnitError(str(exc)) from exc
