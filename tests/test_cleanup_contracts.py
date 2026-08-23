"""Regression coverage for the cleanup's shared execution contracts."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from datatrawl import cli, instruments, registry
from datatrawl.interfaces import (
    Analyzer,
    DataSource,
    PluginInfo,
    READY,
    STUB,
    RunContext,
    STREAM_COMPLEX_BASEBAND,
)
from datatrawl.validation import validate_pipeline, validate_plugin_class


class _Instrument:
    name = "chime"


class _ReadySource(DataSource):
    info = PluginInfo(
        name="test-source",
        kind="source",
        summary="test",
        status=READY,
        instruments=("chime",),
    )

    def enumerate(self, ctx):
        return []

    def fetch(self, unit, dest):
        return False, "not used"


class _StubAnalyzer(Analyzer):
    info = PluginInfo(
        name="test-stub",
        kind="analyzer",
        summary="test",
        status=STUB,
        instruments=("chime",),
        accepts_stream_kinds=(STREAM_COMPLEX_BASEBAND,),
    )


class _WrongInstrumentAnalyzer(Analyzer):
    info = PluginInfo(
        name="test-gbo-only",
        kind="analyzer",
        summary="test",
        status=READY,
        instruments=("gbo",),
        accepts_stream_kinds=(STREAM_COMPLEX_BASEBAND,),
    )


class _FailingPreflightSource(_ReadySource):
    def preflight(self, ctx):
        return False, ["missing test prerequisite"]


def _ctx(**options):
    return RunContext(instrument=_Instrument(), options=options)


def test_execution_validation_rejects_stub_and_wrong_instrument():
    stub = validate_plugin_class("analyzer", _StubAnalyzer, _ctx())
    wrong = validate_plugin_class("analyzer", _WrongInstrumentAnalyzer, _ctx())
    assert not stub.ok and any("stub" in message for message in stub.errors)
    assert not wrong.ok and any("not instrument 'chime'" in message
                                for message in wrong.errors)


def test_execution_validation_runs_preflight_before_work():
    report = validate_pipeline(_ctx(), source_cls=_FailingPreflightSource)
    assert not report.ok
    assert report.errors == ["missing test prerequisite"]


def test_doctor_cannot_certify_unloadable_instrument(monkeypatch, capsys):
    registry.load_plugins()
    monkeypatch.setattr(
        instruments, "load_instrument",
        lambda name: (_ for _ in ()).throw(KeyError("missing band")),
    )
    rc = cli.main([
        "doctor", "--telescope", "chime", "--source", "local",
        "--source-root", os.getcwd(), "--reader", "chime-baseband",
        "--analyzer", "spectrum",
    ])
    output = capsys.readouterr().out
    assert rc == 1
    assert "missing band" in output
    assert "NOT READY" in output


def test_scan_runs_preflight_before_enumeration(tmp_path, monkeypatch, capsys):
    registry.load_plugins()
    source_cls = registry.get("source", "local")

    def should_not_enumerate(self, ctx):
        raise AssertionError("source enumerated before preflight passed")

    monkeypatch.setattr(source_cls, "enumerate", should_not_enumerate)
    missing = tmp_path / "does-not-exist"
    rc = cli.main([
        "scan", "--telescope", "chime", "--source", "local",
        "--source-root", str(missing), "--reader", "chime-baseband",
        "--analyzer", "spectrum", "--select", "1", "--dry-run",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "not a directory" in captured.err


def test_scan_with_no_matching_units_is_not_reported_complete(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = cli.main([
        "scan", "--telescope", "chime", "--source", "local",
        "--source-root", str(empty), "--reader", "chime-baseband",
        "--analyzer", "spectrum", "--select", "1", "--root", str(tmp_path),
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "scan complete: 0/1 product(s)" in captured.out
    assert "1 product(s) not created" in captured.out


def test_scan_with_only_corrupt_input_does_not_claim_product(tmp_path, capsys):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "baseband_1_1.h5").write_bytes(b"not hdf5")
    output = tmp_path / "product.npz"
    rc = cli.main([
        "scan", "--telescope", "chime", "--source", "local",
        "--source-root", str(inputs), "--reader", "chime-baseband",
        "--analyzer", "spectrum", "--select", "1", "--root", str(tmp_path),
        "--out", str(output),
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert not output.exists()
    assert "scan complete: 0/1 product(s)" in captured.out


def test_set_options_cannot_replace_core_options_and_preserve_null():
    args = SimpleNamespace(root="safe", set_opts=["root=elsewhere"])
    with pytest.raises(SystemExit, match="cannot replace reserved"):
        cli._collect_options(args)

    args = SimpleNamespace(root="safe", set_opts=["threshold=null"])
    options = cli._collect_options(args)
    assert "threshold" in options and options["threshold"] is None
    assert "plugin_options" not in options

    with pytest.raises(SystemExit, match="provided more than once"):
        cli._parse_set_options(["threshold=1", "threshold=2"])


def test_default_product_path_cannot_escape_managed_results(tmp_path):
    args = SimpleNamespace(root=str(tmp_path), analyzer="spectrum")
    instrument = SimpleNamespace(name="chime")
    first = cli._default_product_path(args, instrument, ["../../escape"])
    second = cli._default_product_path(args, instrument, ["../../escape"])
    base = tmp_path / "results" / "chime" / "spectrum"
    assert os.path.commonpath([str(base), os.path.abspath(first)]) == str(base)
    assert first == second
    assert os.path.basename(first) not in {".npz", "..npz"}


def test_registry_rejects_wrong_kind_and_unsafe_name():
    class WrongKind:
        info = PluginInfo(name="wrong", kind="reader", summary="test")

    class UnsafeName:
        info = PluginInfo(name="../escape", kind="source", summary="test")

    with pytest.raises(TypeError, match="declares PluginInfo.kind"):
        registry._register({}, WrongKind, "source")
    with pytest.raises(TypeError, match="invalid source plugin name"):
        registry._register({}, UnsafeName, "source")


def test_external_plugin_module_names_do_not_alias_punctuation(tmp_path):
    dashed = os.path.abspath(tmp_path / "plugin-a.py")
    underscored = os.path.abspath(tmp_path / "plugin_a.py")
    assert (
        registry._external_module_name(dashed)
        != registry._external_module_name(underscored)
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("name: bad\n", "band must be a mapping"),
        (
            "name: bad\nband: {f0_mhz: 1, bandwidth_mhz: 1, "
            "n_channels: 1, descending: 'false'}\nnyquist_zone: 1\n",
            "descending must be YAML true or false",
        ),
        (
            "name: bad\nband: {f0_mhz: 1, bandwidth_mhz: 1, "
            "n_channels: 1.5}\nnyquist_zone: 1\n",
            "n_channels must be an integer",
        ),
        (
            "name: bad\nband: {f0_mhz: 1, bandwidth_mhz: 1, "
            "n_channels: 1}\nsense: -1\nnyquist_zone: 1\n",
            "unsupported configuration key 'sense'",
        ),
        (
            "name: other\nband: {f0_mhz: 1, bandwidth_mhz: 1, "
            "n_channels: 1}\nnyquist_zone: 1\n",
            "filename and configured name must match",
        ),
    ],
)
def test_instrument_schema_rejects_ambiguous_or_incomplete_yaml(
        tmp_path, body, message):
    (tmp_path / "bad.yaml").write_text(body)
    with pytest.raises(ValueError, match=message):
        instruments.load_instrument("bad", directory=str(tmp_path))
    readiness = instruments.all_readiness(directory=str(tmp_path))
    assert len(readiness) == 1
    assert readiness[0].status == "invalid"
    assert readiness[0].problems
