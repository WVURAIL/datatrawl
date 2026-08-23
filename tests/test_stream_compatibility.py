"""Reader -> analyzer stream-contract regression tests.

The contract is intentionally metadata-only: it prevents semantic mismatches
and incomplete plugin declarations before files are enumerated or staged.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from datatrawl import cli, pipeline, registry
from datatrawl.interfaces import (
    PluginInfo,
    READY,
    STREAM_ANY,
    STREAM_COMPLEX_BASEBAND,
    STREAM_COMPLEX_GAINS,
    STREAM_VISIBILITY_CHUNK,
    stream_compatibility,
)
from datatrawl.validation import validate_plugin_class


def _reader(name: str, kind: str = "") -> PluginInfo:
    return PluginInfo(
        name=name,
        kind="reader",
        summary="test reader",
        stream_kind=kind,
    )


def _analyzer(name: str, *accepted: str) -> PluginInfo:
    return PluginInfo(
        name=name,
        kind="analyzer",
        summary="test analyzer",
        accepts_stream_kinds=tuple(accepted),
    )


def test_plugin_info_without_stream_declaration_is_incomplete():
    info = PluginInfo(
        "incomplete-reader", "reader", "test", READY, ("*",), "", (), "", False
    )
    assert info.stream_kind == ""
    assert info.accepts_stream_kinds == ()


@pytest.mark.parametrize(
    ("kind", "info", "message"),
    [
        ("reader", _reader("incomplete-reader"), "stream_kind"),
        ("analyzer", _analyzer("incomplete-analyzer"),
         "accepts_stream_kinds"),
    ],
)
def test_execution_validation_rejects_incomplete_contract(
        kind, info, message):
    plugin_class = type("IncompletePlugin", (), {"info": info})
    report = validate_plugin_class(
        kind, plugin_class, ctx=None, run_preflight=False)
    assert not report.ok
    assert any(message in error for error in report.errors)


@pytest.mark.parametrize(
    ("reader_kind", "accepted", "expected"),
    [
        (STREAM_COMPLEX_BASEBAND, (STREAM_COMPLEX_BASEBAND,), True),
        (STREAM_COMPLEX_GAINS, (STREAM_COMPLEX_BASEBAND,), False),
        (STREAM_VISIBILITY_CHUNK, (STREAM_COMPLEX_BASEBAND,), False),
        (STREAM_COMPLEX_GAINS,
         (STREAM_COMPLEX_BASEBAND, STREAM_COMPLEX_GAINS), True),
        ("", (STREAM_COMPLEX_BASEBAND,), False),
        (STREAM_COMPLEX_BASEBAND, (), False),
        ("", (STREAM_ANY,), False),
    ],
)
def test_stream_compatibility_requires_complete_declarations(
        reader_kind, accepted, expected):
    result = stream_compatibility(
        _reader("reader", reader_kind), _analyzer("analyzer", *accepted)
    )
    assert result.compatible is expected


def test_ready_combos_only_certify_declared_matches():
    registry.load_plugins()
    combos = cli._ready_combos()

    assert any(reader == "chime-baseband" and analyzer == "spectrum"
               for _, _, reader, analyzer in combos)
    assert all(reader not in {"outrigger-gains", "outrigger-n2"}
               for _, _, reader, analyzer in combos
               if analyzer == "spectrum")


def test_selected_doctor_accepts_baseband_spectrum(tmp_path, capsys):
    rc = cli.main([
        "doctor", "--telescope", "chime", "--source", "local",
        "--source-root", str(tmp_path), "--reader", "chime-baseband",
        "--analyzer", "spectrum",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[OK] reader/analyzer stream contract" in out
    assert "READY: all checks passed" in out


@pytest.mark.parametrize("reader", ["outrigger-gains", "outrigger-n2"])
def test_selected_doctor_rejects_mismatched_stream(
        reader, tmp_path, capsys, monkeypatch):
    registry.load_plugins()
    if reader == "outrigger-n2":
        # Isolate the semantic contract from the optional compression dependency.
        cls = registry.get("reader", reader)
        monkeypatch.setattr(cls, "preflight", lambda self, ctx: (True, []))

    rc = cli.main([
        "doctor", "--telescope", "gbo", "--source", "local",
        "--source-root", str(tmp_path), "--reader", reader,
        "--analyzer", "spectrum",
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[ ] reader/analyzer stream contract" in out
    assert "complex-baseband-frame" in out
    assert "NOT READY" in out


def test_scan_rejects_mismatch_before_source_enumeration(
        tmp_path, monkeypatch, capsys):
    registry.load_plugins()
    source_cls = registry.get("source", "local")

    def should_not_enumerate(self, ctx):
        raise AssertionError("incompatible scan enumerated its source")

    monkeypatch.setattr(source_cls, "enumerate", should_not_enumerate)
    rc = cli.main([
        "scan", "--telescope", "gbo", "--source", "local",
        "--source-root", str(tmp_path), "--reader", "outrigger-gains",
        "--analyzer", "spectrum", "--select", "1", "--dry-run",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "complex-gain-solution" in captured.err
    assert not (tmp_path / "results").exists()


def test_pipeline_direct_use_rejects_mismatch_before_consuming_units(tmp_path):
    reader = SimpleNamespace(info=_reader("gain-reader", STREAM_COMPLEX_GAINS))
    analyzer = SimpleNamespace(
        info=_analyzer("spectrum-like", STREAM_COMPLEX_BASEBAND)
    )
    source = SimpleNamespace(
        info=PluginInfo(name="source", kind="source", summary="test")
    )

    def units():
        raise AssertionError("pipeline materialized units before contract check")
        yield  # pragma: no cover

    with pytest.raises(SystemExit, match="incompatible reader/analyzer stream"):
        pipeline.run(
            source=source,
            reader=reader,
            analyzer=analyzer,
            units=units(),
            out_path=str(tmp_path / "product.npz"),
            tmp_dir=str(tmp_path / "stage"),
            ctx=SimpleNamespace(options={}),
            verbose=False,
        )


def test_pipeline_rejects_incomplete_stream_contract_before_units(tmp_path):
    reader = SimpleNamespace(info=_reader("incomplete-reader"))
    analyzer = SimpleNamespace(
        info=_analyzer("incomplete-analyzer"),
    )
    source = SimpleNamespace(
        info=PluginInfo(name="source", kind="source", summary="test")
    )

    def units():
        raise AssertionError("incomplete contract materialized its units")
        yield  # pragma: no cover

    with pytest.raises(SystemExit, match="stream contract is incomplete"):
        pipeline.run(
            source=source,
            reader=reader,
            analyzer=analyzer,
            units=units(),
            out_path=str(tmp_path / "product.npz"),
            tmp_dir=str(tmp_path / "stage"),
            ctx=SimpleNamespace(options={}),
            verbose=False,
        )
