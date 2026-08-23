"""Focused command-boundary tests for survey and explore validation."""
from __future__ import annotations

import os

import pytest

from datatrawl import cli, registry
from datatrawl.interfaces import DataSource, PluginInfo, READY, STUB
from datatrawl.plugins.sources import _datatrail


def _cadc_source_class():
    registry.load_plugins()
    return registry.get("source", "cadc-datatrail")


def test_survey_dry_run_skips_construction_and_prerequisite_checks(
        tmp_path, monkeypatch, capsys):
    source_cls = _cadc_source_class()

    def unexpected(*args, **kwargs):
        raise AssertionError("dry-run contacted or constructed a dependency")

    monkeypatch.setattr(source_cls, "preflight", unexpected)
    monkeypatch.setattr(source_cls, "__init__", unexpected)
    monkeypatch.setattr(
        _datatrail.Datatrail, "installed", staticmethod(unexpected))

    rc = cli.main([
        "survey", "--telescope", "chime", "--freq-ids", "1",
        "--out", str(tmp_path / "inventory"), "--dry-run",
    ])

    captured = capsys.readouterr()
    assert rc == 0
    assert "dry-run: would survey" in captured.out
    assert not (tmp_path / "inventory").exists()


def test_event_survey_requires_datatrail_even_when_source_preflight_is_silent(
        tmp_path, monkeypatch, capsys):
    source_cls = _cadc_source_class()
    monkeypatch.setattr(source_cls, "preflight", lambda self, ctx: (True, []))
    monkeypatch.setattr(
        _datatrail.Datatrail, "installed", staticmethod(lambda: False))
    monkeypatch.setattr(
        source_cls, "survey",
        lambda self, ctx, out: pytest.fail("survey ran after failed preflight"),
    )

    rc = cli.main([
        "survey", "--telescope", "chime", "--freq-ids", "1",
        "--out", str(tmp_path / "inventory"),
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "datatrail-cli is required" in captured.err
    assert "[survey]" not in captured.out


def test_event_survey_runs_cadc_source_preflight_before_work(
        tmp_path, monkeypatch, capsys):
    source_cls = _cadc_source_class()
    monkeypatch.setattr(
        source_cls, "preflight",
        lambda self, ctx: (False, ["CADC certificate is unavailable"]),
    )
    monkeypatch.setattr(
        _datatrail.Datatrail, "installed", staticmethod(lambda: True))
    monkeypatch.setattr(
        source_cls, "survey",
        lambda self, ctx, out: pytest.fail("survey ran after failed preflight"),
    )

    rc = cli.main([
        "survey", "--telescope", "chime", "--freq-ids", "1",
        "--out", str(tmp_path / "inventory"),
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "CADC certificate is unavailable" in captured.err


@pytest.mark.parametrize(
    ("strict", "issues", "expected_rc"),
    [
        (False, {"refused": 1}, 0),
        (True, {"refused": 1}, 1),
        (True, {}, 0),
    ],
)
def test_survey_strict_completeness_controls_exit_after_preserving_output(
        tmp_path, monkeypatch, capsys, strict, issues, expected_rc):
    source_cls = _cadc_source_class()
    monkeypatch.setattr(source_cls, "preflight", lambda self, ctx: (True, []))
    monkeypatch.setattr(
        _datatrail.Datatrail, "installed", staticmethod(lambda: True))

    def fake_survey(self, ctx, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        inventory = os.path.join(out_dir, "inventory.jsonl")
        with open(inventory, "w"):
            pass
        return inventory

    monkeypatch.setattr(source_cls, "survey", fake_survey)
    monkeypatch.setattr(
        source_cls, "survey_completeness_issues",
        lambda self, out_dir: dict(issues),
    )
    out = tmp_path / f"inventory-{strict}-{expected_rc}"
    argv = [
        "survey", "--telescope", "chime", "--freq-ids", "1",
        "--out", str(out),
    ]
    if strict:
        argv.append("--strict-completeness")

    rc = cli.main(argv)

    captured = capsys.readouterr()
    assert rc == expected_rc
    assert (out / "inventory.jsonl").exists()
    assert (out / "inventory.meta.json").exists()
    if strict and issues:
        assert "strict survey completeness failed" in captured.err
        assert "1 refused" in captured.err
    elif strict:
        assert "strict completeness: no unresolved" in captured.out


def test_strict_completeness_rejects_scope_recon_before_work(
        tmp_path, monkeypatch, capsys):
    source_cls = _cadc_source_class()
    monkeypatch.setattr(
        source_cls, "survey",
        lambda self, ctx, out: pytest.fail("scope recon ran in strict mode"),
    )

    rc = cli.main([
        "survey", "--scopes-only", "--strict-completeness",
        "--out", str(tmp_path),
    ])

    assert rc == 2
    assert "applies to event surveys" in capsys.readouterr().err


def test_scopes_only_checks_datatrail_without_running_cadc_preflight(
        tmp_path, monkeypatch, capsys):
    source_cls = _cadc_source_class()

    def unexpected_preflight(self, ctx):
        raise AssertionError("recon ran the CADC-oriented source preflight")

    monkeypatch.setattr(source_cls, "preflight", unexpected_preflight)
    monkeypatch.setattr(
        _datatrail.Datatrail, "installed", staticmethod(lambda: True))
    monkeypatch.setattr(
        _datatrail.Datatrail, "api_available",
        staticmethod(lambda: (True, "")),
    )
    output = tmp_path / "scopes.jsonl"
    monkeypatch.setattr(
        source_cls, "survey", lambda self, ctx, out: str(output))

    rc = cli.main(["survey", "--scopes-only", "--out", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 0
    assert f"scope map: {output}" in captured.out


def test_scopes_only_fails_promptly_without_datatrail(
        tmp_path, monkeypatch, capsys):
    source_cls = _cadc_source_class()
    monkeypatch.setattr(
        _datatrail.Datatrail, "installed", staticmethod(lambda: False))
    monkeypatch.setattr(
        source_cls, "survey",
        lambda self, ctx, out: pytest.fail("recon ran without datatrail"),
    )

    rc = cli.main(["survey", "--scopes-only", "--out", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 2
    assert "datatrail-cli is required" in captured.err


def test_explore_rejects_set_collision_with_explicit_core_option(tmp_path):
    with pytest.raises(SystemExit, match="cannot replace reserved"):
        cli.main([
            "explore", "--source", "local", "--source-root", str(tmp_path),
            "--set", "source_root=/different/input",
        ])


@pytest.mark.parametrize(
    ("status", "instruments", "message"),
    [
        (STUB, ("chime",), "stub and cannot run"),
        (READY, ("gbo",), "not instrument 'chime'"),
    ],
)
def test_explore_applies_source_status_and_instrument_validation(
        tmp_path, monkeypatch, capsys, status, instruments, message):
    class RejectedSource(DataSource):
        info = PluginInfo(
            name="cli-rejected", kind="source", summary="test",
            status=status, instruments=instruments,
        )

        def __init__(self):
            raise AssertionError("rejected source was constructed")

    real_get = registry.get

    def fake_get(kind, name):
        if kind == "source" and name == "cli-rejected":
            return RejectedSource
        return real_get(kind, name)

    monkeypatch.setattr(registry, "get", fake_get)
    rc = cli.main([
        "explore", "--source", "cli-rejected", "--telescope", "chime",
        "--source-root", os.fspath(tmp_path),
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert message in captured.err


def test_explore_rejects_invalid_instrument(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.inst_mod, "load_instrument",
        lambda name: (_ for _ in ()).throw(ValueError("invalid geometry")),
    )

    rc = cli.main([
        "explore", "--source", "local", "--telescope", "broken",
        "--source-root", str(tmp_path),
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "invalid telescope 'broken'" in captured.err
    assert "invalid geometry" in captured.err


def test_scan_reports_output_lock_without_traceback(tmp_path, monkeypatch, capsys):
    source_root = tmp_path / "input"
    source_root.mkdir()
    (source_root / "baseband_1_844.h5").write_bytes(b"staged")

    def locked(**kwargs):
        raise cli.pipeline.OutputLockedError("product is already locked")

    monkeypatch.setattr(cli.pipeline, "run", locked)
    rc = cli.main([
        "scan",
        "--telescope", "chime",
        "--source", "local",
        "--reader", "chime-baseband",
        "--analyzer", "spectrum",
        "--select", "844",
        "--source-root", str(source_root),
        "--out", str(tmp_path / "product.npz"),
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "already locked" in captured.err
    assert "Traceback" not in captured.err


def test_scan_reports_quarantine_lock_without_traceback(
        tmp_path, monkeypatch, capsys):
    source_root = tmp_path / "input"
    source_root.mkdir()
    (source_root / "baseband_1_844.h5").write_bytes(b"staged")

    def locked(**kwargs):
        raise cli.pipeline.QuarantineLedgerLockError(
            "quarantine filesystem does not support advisory locks")

    monkeypatch.setattr(cli.pipeline, "run", locked)
    rc = cli.main([
        "scan",
        "--telescope", "chime",
        "--source", "local",
        "--reader", "chime-baseband",
        "--analyzer", "spectrum",
        "--select", "844",
        "--source-root", str(source_root),
        "--out", str(tmp_path / "product.npz"),
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "does not support advisory locks" in captured.err
    assert "Traceback" not in captured.err
