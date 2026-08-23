"""Strict inventory schema and identity for the CADC Datatrail source.

The source owns archive identity and verified URI fields; readers own only the
relative filenames and product-specific columns. Keeping those rules here gives
survey and enumeration one validation boundary without coupling them to the
survey's network or persistence machinery.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from numbers import Integral


RESERVED_FIELDS = frozenset({
    "scope", "event", "name", "size_bytes", "common_path", "obs_date",
    "datasets",
})
_REQUIRED_FIELDS = frozenset({
    "scope", "event", "name", "size_bytes", "common_path",
})


def join_uri(common_path, name) -> str:
    """Join a verified common path and canonical relative archive name."""
    return f"{str(common_path).rstrip('/')}/{str(name).lstrip('/')}"


def _safe_archive_name(name: str) -> bool:
    """Whether a verified filename is a canonical relative POSIX path."""
    if (not name or name != name.strip() or "\\" in name or "\x00" in name
            or name.startswith("/")):
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def candidate_file(item, reader_name: str) -> tuple[str, dict]:
    """Validate one reader-owned archive candidate before any CADC request."""
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        raise SystemExit(
            f"reader {reader_name!r} survey_files() must yield "
            f"(relative_name, fields) pairs; got {item!r}")
    name, fields = item
    if not isinstance(name, str):
        raise SystemExit(
            f"reader {reader_name!r} yielded non-string archive name "
            f"{name!r}; names must be non-empty paths relative to the event "
            "common path")
    if not _safe_archive_name(name):
        raise SystemExit(
            f"reader {reader_name!r} yielded unsafe archive name {name!r}; "
            "names must be non-empty paths relative to the event common path")
    if fields is None:
        fields = {}
    if not isinstance(fields, Mapping):
        raise SystemExit(
            f"reader {reader_name!r} yielded non-mapping inventory fields for "
            f"{name!r}: {type(fields).__name__}")
    overlap = RESERVED_FIELDS.intersection(fields)
    if overlap:
        raise SystemExit(
            f"reader {reader_name!r} tried to overwrite source-owned inventory "
            f"field(s) {sorted(overlap)} for {name!r}")
    return name, dict(fields)


def annotate_row(shape, row: dict, instrument) -> None:
    """Run reader annotation while protecting source-verified identity."""
    before = {key: deepcopy(row[key]) for key in RESERVED_FIELDS}
    shape.annotate_row(row, instrument)
    changed = [key for key, value in before.items()
               if key not in row or row[key] != value]
    if changed:
        reader_name = getattr(getattr(shape, "info", None), "name",
                              type(shape).__name__)
        raise SystemExit(
            f"reader {reader_name!r} annotate_row() changed source-owned "
            f"inventory field(s) {sorted(changed)}")


def logical_unit_key(scope, event, name) -> str:
    """Archive-location-independent identity used by resume and quarantine."""
    return "cadc-datatrail:" + json.dumps(
        [str(scope), str(event), str(name)], separators=(",", ":"))


def _inventory_error(path: str, line_number: int, detail: str) -> SystemExit:
    return SystemExit(
        f"invalid inventory row {path}:{line_number}: {detail}. Rebuild this "
        "inventory with `datatrawl survey`; legacy or partial rows cannot be "
        "scanned safely.")


def parse_row(text: str, path: str, line_number: int) -> dict:
    """Parse one current-schema row, failing closed with its exact location."""
    try:
        row = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _inventory_error(path, line_number, f"malformed JSON ({exc})")
    if not isinstance(row, dict):
        raise _inventory_error(
            path, line_number, f"expected a JSON object, got {type(row).__name__}")
    missing = sorted(_REQUIRED_FIELDS - set(row))
    if missing:
        raise _inventory_error(path, line_number,
                               f"missing required field(s) {missing}")
    for field in ("scope", "event", "name", "common_path"):
        if (not isinstance(row[field], str) or not row[field].strip()
                or row[field] != row[field].strip()):
            raise _inventory_error(
                path, line_number,
                f"{field!r} must be a non-empty, unpadded string")
    if not _safe_archive_name(row["name"]):
        raise _inventory_error(
            path, line_number,
            "'name' must be a canonical relative path below the common path")
    if (isinstance(row["size_bytes"], bool)
            or not isinstance(row["size_bytes"], Integral)
            or row["size_bytes"] <= 0):
        raise _inventory_error(
            path, line_number, "'size_bytes' must be a positive integer")
    if "freq_id" in row and (
            isinstance(row["freq_id"], bool)
            or not isinstance(row["freq_id"], Integral)
            or row["freq_id"] < 0):
        raise _inventory_error(
            path, line_number, "'freq_id' must be a non-negative integer")
    datasets = row.get("datasets")
    if datasets is not None and (
            not isinstance(datasets, list)
            or any(not isinstance(label, str) or not label.strip()
                   for label in datasets)):
        raise _inventory_error(
            path, line_number, "'datasets' must be a list of non-empty strings")
    return row
