"""One execution-eligibility contract shared by CLI commands and the engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .interfaces import (
    Analyzer,
    DataSource,
    EXPERIMENTAL,
    READY,
    Reader,
    STUB,
    RunContext,
    stream_compatibility,
)


_VALID_KINDS = {"source", "reader", "analyzer"}
_VALID_STATUSES = {READY, EXPERIMENTAL, STUB}
_REQUIRED_METHODS = {
    "source": (DataSource, ("enumerate", "fetch")),
    "reader": (Reader, ("probe", "iter_arrays")),
    "analyzer": (
        Analyzer,
        ("resume", "processed_keys", "begin", "consume_file", "save"),
    ),
}


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.notes.extend(other.notes)


def _instrument_name(ctx: Optional[RunContext]) -> Optional[str]:
    instrument = getattr(ctx, "instrument", None) if ctx is not None else None
    name = getattr(instrument, "name", None)
    return str(name) if name else None


def validate_plugin_class(kind: str, cls: type, ctx: Optional[RunContext],
                          *, run_preflight: bool = True) -> ValidationReport:
    """Validate metadata, instrument support, construction, and prerequisites."""
    report = ValidationReport()
    info = getattr(cls, "info", None)
    if info is None:
        report.errors.append(f"{kind} class {cls.__name__} has no PluginInfo")
        return report
    if kind not in _VALID_KINDS:
        report.errors.append(f"unknown plugin kind {kind!r}")
    if getattr(info, "kind", None) != kind:
        report.errors.append(
            f"plugin {info.name!r} is registered as {kind!r} but declares "
            f"kind={getattr(info, 'kind', None)!r}")
    status = getattr(info, "status", None)
    if status not in _VALID_STATUSES:
        report.errors.append(f"plugin {info.name!r} has invalid status {status!r}")
    elif status == STUB:
        report.errors.append(f"{kind} {info.name!r} is a stub and cannot run")
    elif status == EXPERIMENTAL:
        report.warnings.append(f"{kind} {info.name!r} is experimental")

    base_and_methods = _REQUIRED_METHODS.get(kind)
    if base_and_methods is not None:
        base, methods = base_and_methods
        missing_methods = []
        for method in methods:
            implementation = getattr(cls, method, None)
            if (not callable(implementation)
                    or implementation is getattr(base, method)):
                missing_methods.append(method)
        if missing_methods:
            report.errors.append(
                f"{kind} {info.name!r} does not implement required method(s) "
                f"{missing_methods!r}")

    if kind == "reader":
        stream_kind = getattr(info, "stream_kind", None)
        if not isinstance(stream_kind, str) or not stream_kind.strip():
            report.errors.append(
                f"reader {info.name!r} must declare a non-empty stream_kind")
    elif kind == "analyzer":
        accepted = getattr(info, "accepts_stream_kinds", None)
        if (not isinstance(accepted, tuple) or not accepted
                or any(not isinstance(item, str) or not item.strip()
                       for item in accepted)):
            report.errors.append(
                f"analyzer {info.name!r} must declare non-empty "
                "accepts_stream_kinds")
        elif len(set(accepted)) != len(accepted):
            report.errors.append(
                f"analyzer {info.name!r} accepts_stream_kinds contains duplicates")

    instrument_name = _instrument_name(ctx)
    declared = tuple(getattr(info, "instruments", ()) or ())
    if instrument_name and declared and "*" not in declared:
        if instrument_name not in declared:
            report.errors.append(
                f"{kind} {info.name!r} supports {list(declared)!r}, not "
                f"instrument {instrument_name!r}")
    elif instrument_name and not declared:
        report.warnings.append(
            f"{kind} {info.name!r} does not declare supported instruments")

    if not run_preflight or ctx is None or report.errors:
        return report
    try:
        plugin = cls()
    except Exception as exc:
        report.errors.append(
            f"could not construct {kind} {info.name!r}: "
            f"{type(exc).__name__}: {exc}")
        return report
    try:
        result = plugin.preflight(ctx)
        ok, problems = bool(result[0]), list(result[1])
        notes = list(result[2]) if len(result) > 2 else []
    except Exception as exc:
        report.errors.append(
            f"{kind} {info.name!r} preflight failed: "
            f"{type(exc).__name__}: {exc}")
        return report
    report.notes.extend(str(note) for note in notes)
    report.errors.extend(str(problem) for problem in problems)
    if not ok and not problems:
        report.errors.append(f"{kind} {info.name!r} preflight did not pass")
    return report


def validate_pipeline(ctx: RunContext, *, source_cls: Optional[type] = None,
                      reader_cls: Optional[type] = None,
                      analyzer_cls: Optional[type] = None,
                      run_preflight: bool = True) -> ValidationReport:
    """Validate selected components and their reader/analyzer stream contract."""
    report = ValidationReport()
    for kind, cls in (("source", source_cls), ("reader", reader_cls),
                      ("analyzer", analyzer_cls)):
        if cls is not None:
            report.extend(validate_plugin_class(
                kind, cls, ctx, run_preflight=run_preflight))
    reader_info = getattr(reader_cls, "info", None)
    analyzer_info = getattr(analyzer_cls, "info", None)
    if reader_info is not None and analyzer_info is not None:
        contract = stream_compatibility(reader_info, analyzer_info)
        if not contract.compatible:
            report.errors.append(contract.detail)
    return report


def format_report(report: ValidationReport) -> Iterable[tuple[str, str]]:
    """Yield ``(level, message)`` pairs for a CLI renderer."""
    yield from (("error", message) for message in report.errors)
    yield from (("warning", message) for message in report.warnings)
    yield from (("note", message) for message in report.notes)
