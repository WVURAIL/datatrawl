"""Inventory naming and metadata resolution used by CLI commands.

``survey`` writes a small ``<inventory>.meta.json`` sidecar recording how an
inventory was built. ``explore`` and ``scan`` read that sidecar to recover the
instrument, source, and reader without duplicating command-line flags. Keeping
that path policy here makes it independently testable and keeps command
orchestration out of the metadata implementation.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from . import invpaths
from .names import validate_identifier


_MAX_INVENTORY_SLUG_LENGTH = 40
_INVENTORY_SLUG_HASH_LENGTH = 8
INVENTORY_META_SCHEMA_KEY = "datatrawl_inventory"
INVENTORY_META_SCHEMA_VERSION = 1


def _meta_path_for(inventory_path: str) -> str:
    return os.path.splitext(inventory_path)[0] + ".meta.json"


def _freq_id_slug(freq_ids) -> str:
    """Return a short, filesystem-safe selection slug (empty means all)."""
    if not freq_ids or str(freq_ids).strip().lower() in ("all", "*"):
        return ""
    slug = str(freq_ids).strip().lower().replace(" ", "").replace(",", "-")
    slug = re.sub(r"[^a-z0-9._-]", "", slug)
    if slug and slug[0].isdigit():
        slug = "fid" + slug
    if len(slug) > _MAX_INVENTORY_SLUG_LENGTH:
        digest = hashlib.sha256(slug.encode()).hexdigest()
        slug = "fid" + digest[:_INVENTORY_SLUG_HASH_LENGTH]
    return slug


def derive_inventory_name(instrument_name: str, freq_ids) -> str:
    """Derive the deterministic default directory name for one survey spec."""
    instrument_name = validate_identifier(
        instrument_name, label="instrument name")
    slug = _freq_id_slug(freq_ids)
    return f"{instrument_name}-{slug}" if slug else instrument_name


def _scopes_in_inventory(inventory_path) -> list:
    """Return unique row scopes in first-seen order."""
    seen: list = []
    with open(inventory_path, encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{inventory_path}:{line_number}: invalid inventory JSON: "
                    f"{exc.msg}") from exc
            if not isinstance(row, Mapping):
                raise ValueError(
                    f"{inventory_path}:{line_number}: expected a JSON object, "
                    f"got {type(row).__name__}")
            scope = row.get("scope")
            if scope is not None and not isinstance(scope, str):
                raise ValueError(
                    f"{inventory_path}:{line_number}: inventory field 'scope' "
                    f"must be a string, got {type(scope).__name__}")
            if scope and scope not in seen:
                seen.append(scope)
    return seen


def write_inventory_meta(inventory_path, instrument, source, freq_ids=None,
                         name=None, scope_request=None, reader=None) -> str:
    """Write the provenance sidecar next to an inventory."""
    scopes = _scopes_in_inventory(inventory_path)
    if not scopes:
        if scope_request:
            scopes = [scope.strip() for scope in scope_request.split(",")
                      if scope.strip()]
        elif getattr(instrument, "scopes", None):
            scopes = list(instrument.scopes)
    meta = {
        INVENTORY_META_SCHEMA_KEY: INVENTORY_META_SCHEMA_VERSION,
        "name": name,
        "telescope": instrument.name,
        "source": source,
        "reader": reader or getattr(instrument, "reader", "") or None,
        "scope": ",".join(scopes) if scopes else None,
        "scopes": scopes or None,
        "scope_request": scope_request or None,
        "freq_ids": freq_ids,
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    meta_path = _meta_path_for(inventory_path)
    directory = os.path.dirname(os.path.abspath(meta_path))
    fd, temporary = tempfile.mkstemp(
        prefix=".inventory-meta-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(meta, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, meta_path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
    return meta_path


def _sole_inventory(root: Optional[str]):
    """Return the sole inventory under an explicit or canonical root."""
    base = (Path(root).expanduser().resolve() / "data"
            if root else invpaths.inventory_root())
    hits = sorted(set(glob.glob(str(base / "*" / "inventory.jsonl"))))
    return hits[0] if len(hits) == 1 else None


def _validated_meta(meta_path: str) -> Mapping:
    """Load one current, safe inventory metadata sidecar."""
    try:
        with open(meta_path, encoding="utf-8") as stream:
            meta = json.load(stream)
    except OSError as exc:
        raise ValueError(
            f"cannot read inventory metadata {meta_path!r}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(
            f"invalid inventory metadata JSON in {meta_path!r}: {exc}") from exc
    if not isinstance(meta, Mapping):
        raise ValueError(
            f"invalid inventory metadata {meta_path!r}: expected a JSON object")
    schema_version = meta.get(INVENTORY_META_SCHEMA_KEY)
    if (type(schema_version) is not int
            or schema_version != INVENTORY_META_SCHEMA_VERSION):
        raise ValueError(
            f"incompatible inventory metadata {meta_path!r}: "
            f"{INVENTORY_META_SCHEMA_KEY!r} must be exactly "
            f"{INVENTORY_META_SCHEMA_VERSION}, got {schema_version!r}")
    for key in ("telescope", "source"):
        try:
            validate_identifier(meta.get(key), label=f"metadata {key}")
        except ValueError as exc:
            raise ValueError(f"{meta_path}: {exc}") from exc
    reader = meta.get("reader")
    if reader is not None:
        try:
            validate_identifier(reader, label="metadata reader")
        except ValueError as exc:
            raise ValueError(f"{meta_path}: {exc}") from exc
    return meta


def resolve_from_meta(args) -> None:
    """Backfill command arguments from an inventory metadata sidecar.

    Explicit arguments always win. Missing, unreadable, or ambiguous implicit
    inventories are a silent no-op so callers can render their own command-level
    missing-option diagnostics.
    """
    explicit_selection = any(
        getattr(args, key, None) is not None
        for key in ("inventory", "name", "telescope"))
    inventory = getattr(args, "inventory", None)
    root = getattr(args, "root", None)
    if inventory is None and getattr(args, "name", None):
        try:
            name = validate_identifier(args.name, label="inventory name")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        inventory = (os.path.join(root, "data", name, "inventory.jsonl")
                     if root else str(invpaths.resolve_inventory(name)))
    if inventory is None and getattr(args, "telescope", None):
        inventory = (
            os.path.join(root, "data", args.telescope, "inventory.jsonl")
            if root else str(invpaths.resolve_inventory(args.telescope)))
    if inventory is None:
        inventory = _sole_inventory(root)
    if not inventory:
        return
    meta_path = _meta_path_for(inventory)
    if not os.path.exists(meta_path):
        return
    try:
        meta = _validated_meta(meta_path)
    except ValueError as exc:
        if explicit_selection:
            raise SystemExit(str(exc)) from exc
        return
    for key in ("telescope", "source", "reader"):
        if getattr(args, key, None) is None and meta.get(key):
            setattr(args, key, meta[key])
    if getattr(args, "inventory", None) is None:
        args.inventory = inventory
