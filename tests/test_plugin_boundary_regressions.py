"""Focused regressions for plugin, analyzer, reader, and geometry boundaries."""
from __future__ import annotations

import builtins
import importlib.metadata
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from datatrawl import accel, registry
from datatrawl.instruments import load_instrument
from datatrawl.interfaces import (
    Analyzer,
    DataSource,
    PluginInfo,
    READY,
    Reader,
    RunContext,
    UnreadableUnitError,
)
from datatrawl.plugins.analyzers.spectrum import PowerSpectrumAnalyzer
from datatrawl.plugins.readers.chime_baseband import ChimeBasebandReader
from datatrawl.plugins.readers.outrigger_gains_reader import OutriggerGainsReader
from datatrawl.plugins.readers.outrigger_n2_reader import OutriggerN2Reader
from datatrawl.selection import Selection
from datatrawl.validation import validate_pipeline, validate_plugin_class


def _spectrum_ctx(freq_id: int = 844) -> RunContext:
    return RunContext(
        instrument=load_instrument("chime"),
        selection=[freq_id],
        options={},
    )


def test_spectrum_rejects_first_file_from_another_selected_channel():
    ctx = _spectrum_ctx(844)
    wrong_center = ctx.instrument.freq_of_freq_id(706) * 1e6
    with pytest.raises(SystemExit, match="different channel"):
        PowerSpectrumAnalyzer().begin(
            ctx, {"f_center_hz": wrong_center})


def test_spectrum_resume_separates_actual_and_configured_nfft(tmp_path):
    ctx = _spectrum_ctx()
    center = ctx.instrument.freq_of_freq_id(844) * 1e6
    analyzer = PowerSpectrumAnalyzer()
    analyzer.begin(ctx, {"f_center_hz": center})
    assert analyzer.consume_file(
        [np.zeros(8, dtype=np.complex64)],
        {
            "unit_key": "unit-1",
            "unit_name": "unit-1.h5",
            "f_center_hz": center,
        },
    ) == 1
    product = tmp_path / "spectrum.npz"
    analyzer.save(str(product))

    with np.load(product, allow_pickle=False) as saved:
        assert int(saved["nfft"]) == 8
        assert int(saved["configured_nfft"]) == ctx.instrument.nfft
    assert PowerSpectrumAnalyzer().resume(str(product), ctx) is True


@pytest.mark.parametrize("schema", [None, "datatrawl.spectrum/v999"])
def test_spectrum_refuses_unknown_product_schema(tmp_path, schema):
    ctx = _spectrum_ctx()
    center = ctx.instrument.freq_of_freq_id(844) * 1e6
    analyzer = PowerSpectrumAnalyzer()
    analyzer.begin(ctx, {"f_center_hz": center})
    assert analyzer.consume_file(
        [np.zeros(8, dtype=np.complex64)],
        {"unit_key": "one", "unit_name": "one.h5", "f_center_hz": center},
    ) == 1
    product = tmp_path / "spectrum.npz"
    analyzer.save(str(product))
    with np.load(product, allow_pickle=False) as saved:
        fields = {
            name: np.array(saved[name], copy=True)
            for name in saved.files
            if name != "product_schema"
        }
    if schema is not None:
        fields["product_schema"] = np.array(schema)
    np.savez(product, **fields)

    with pytest.raises(SystemExit, match="product_schema"):
        PowerSpectrumAnalyzer().resume(str(product), ctx)


def test_spectrum_rejects_nonfinite_center_on_later_file():
    ctx = _spectrum_ctx()
    center = ctx.instrument.freq_of_freq_id(844) * 1e6
    analyzer = PowerSpectrumAnalyzer()
    analyzer.begin(ctx, {"f_center_hz": center})

    consumed = analyzer.consume_file(
        [np.zeros(8, dtype=np.complex64)],
        {
            "unit_key": "nan-center",
            "unit_name": "nan-center.h5",
            "f_center_hz": np.nan,
        },
    )

    assert consumed == 0
    assert "nan-center" not in analyzer.processed_keys()


def test_spectrum_rejects_nonfinite_center_when_resuming(tmp_path):
    ctx = _spectrum_ctx()
    center = ctx.instrument.freq_of_freq_id(844) * 1e6
    analyzer = PowerSpectrumAnalyzer()
    analyzer.begin(ctx, {"f_center_hz": center})
    assert analyzer.consume_file(
        [np.zeros(8, dtype=np.complex64)],
        {"unit_key": "one", "unit_name": "one.h5", "f_center_hz": center},
    ) == 1
    product = tmp_path / "spectrum.npz"
    analyzer.save(str(product))

    resumed = PowerSpectrumAnalyzer()
    assert resumed.resume(str(product), ctx) is True
    with pytest.raises(SystemExit, match="non-finite f_center_hz"):
        resumed.begin(ctx, {"f_center_hz": np.nan})


def test_failed_path_plugin_import_rolls_back_decorator_mutations(tmp_path):
    plugin = tmp_path / "partial_plugin.py"
    plugin.write_text(
        "from datatrawl.interfaces import Analyzer, PluginInfo\n"
        "from datatrawl.registry import analyzer\n"
        "@analyzer\n"
        "class Partial(Analyzer):\n"
        "    info = PluginInfo(name='partial-import-poison-regression', "
        "kind='analyzer', summary='test')\n"
        "raise RuntimeError('after decorator')\n"
    )
    with pytest.raises(SystemExit, match="after decorator"):
        registry._import_target(str(plugin))
    assert "partial-import-poison-regression" not in registry.available(
        "analyzer")


def test_failed_entry_point_import_rolls_back_decorator_mutations(monkeypatch):
    class PartialEntryPointAnalyzer(Analyzer):
        info = PluginInfo(
            name="partial-entry-point-poison-regression",
            kind="analyzer",
            summary="test",
        )

    class EntryPoint:
        name = "broken-regression"

        def load(self):
            registry.analyzer(PartialEntryPointAnalyzer)
            raise RuntimeError("entry point failed after registration")

    class EntryPoints(list):
        def select(self, **kwargs):
            assert kwargs == {"group": "datatrawl.plugins"}
            return self

    monkeypatch.setattr(registry, "_EP_LOADED", False)
    monkeypatch.setattr(
        importlib.metadata, "entry_points",
        lambda: EntryPoints([EntryPoint()]),
    )
    with pytest.warns(UserWarning, match="failed to load"):
        registry._load_entry_point_plugins()
    assert "partial-entry-point-poison-regression" not in registry.available(
        "analyzer")


def test_validation_rejects_non_callable_required_methods():
    class BrokenSource(DataSource):
        info = PluginInfo(
            name="non-callable-source-regression",
            kind="source",
            summary="test",
            status=READY,
        )
        enumerate = None
        fetch = None

    report = validate_plugin_class(
        "source", BrokenSource, ctx=None, run_preflight=False)
    assert not report.ok
    assert any("enumerate" in error and "fetch" in error
               for error in report.errors)


def test_pipeline_validation_reports_missing_info_without_crashing():
    class MissingInfoReader(Reader):
        def probe(self, path):
            return {}

        def iter_arrays(self, path, ctx):
            return iter(())

    report = validate_pipeline(
        _spectrum_ctx(),
        reader_cls=MissingInfoReader,
        analyzer_cls=PowerSpectrumAnalyzer,
        run_preflight=False,
    )
    assert not report.ok
    assert any("has no PluginInfo" in error for error in report.errors)


@pytest.mark.parametrize("value", [True, False, 1.9, 1.0, "1.9", object()])
def test_selection_matcher_never_truncates_non_integer_metadata(value):
    assert Selection(freq_ids=frozenset({1})).wants_freq_id(value) is False


def test_selection_matcher_accepts_integral_metadata():
    selection = Selection(freq_ids=frozenset({1}))
    assert selection.wants_freq_id(1)
    assert selection.wants_freq_id(np.int64(1))
    assert selection.wants_freq_id("1")


def test_cuda_detection_prefers_installed_toolkit_to_driver(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "nvcc":
            return SimpleNamespace(
                stdout="Cuda compilation tools, release 11.8, V11.8.89",
                stderr="",
            )
        if cmd[0] == "nvidia-smi":
            return SimpleNamespace(stdout="CUDA Version: 12.4", stderr="")
        raise FileNotFoundError(cmd[0])

    monkeypatch.delenv("CUDA_HOME", raising=False)
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.setattr(accel.subprocess, "run", run)
    assert accel.detect_cuda_major() == 11
    assert ["nvidia-smi"] not in calls


def test_cuda_detection_honors_configured_toolkit_before_path(monkeypatch):
    def run(cmd, **kwargs):
        if cmd[0] == "/chosen/cuda/bin/nvcc":
            raise FileNotFoundError(cmd[0])
        if cmd[0] == "nvcc":
            return SimpleNamespace(
                stdout="Cuda compilation tools, release 12.4, V12.4.0",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setenv("CUDA_HOME", "/chosen/cuda")
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.setattr(accel.subprocess, "run", run)
    monkeypatch.setattr(
        accel, "_cuda_major_from_file",
        lambda path: 11 if path == "/chosen/cuda/version.json" else None,
    )
    assert accel.detect_cuda_major() == 11


def test_broken_cupy_install_is_not_reported_as_absent(monkeypatch):
    real_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "cupy":
            raise ModuleNotFoundError(
                "No module named 'cupy_backends'", name="cupy_backends")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    with pytest.raises(RuntimeError, match="installed but could not import"):
        accel.import_cupy()


def test_gains_probe_marks_invalid_filename_as_unreadable(tmp_path):
    path = tmp_path / "not-a-gain-file.h5"
    with h5py.File(path, "w") as handle:
        handle.create_dataset("gain", data=np.zeros((2, 2), np.complex64))
    with pytest.raises(UnreadableUnitError, match="recognized outrigger gains"):
        OutriggerGainsReader().probe(str(path))


def test_gains_reader_rejects_non_complex_payload(tmp_path):
    path = tmp_path / "gain_20250114T093512.5Z_cyga.h5"
    with h5py.File(path, "w") as handle:
        handle.create_dataset("gain", data=np.zeros((2, 2), np.float32))
    reader = OutriggerGainsReader()
    with pytest.raises(UnreadableUnitError, match="complex dataset 'gain'"):
        reader.probe(str(path))
    with pytest.raises(UnreadableUnitError, match="complex dataset 'gain'"):
        list(reader.iter_arrays(str(path), _spectrum_ctx()))


def _write_n2(path, *, dtype=np.complex64, n_times=2, index_times=2):
    time_dtype = np.dtype([("ctime", "f8")])
    with h5py.File(path, "w") as handle:
        handle.create_dataset(
            "vis", data=np.zeros((1024, 3, n_times), dtype=dtype))
        times = np.zeros(index_times, dtype=time_dtype)
        handle.create_dataset("index_map/time", data=times)


def test_n2_reader_rejects_non_complex_visibility(tmp_path):
    path = tmp_path / "n2-real.h5"
    _write_n2(path, dtype=np.float32)
    with pytest.raises(UnreadableUnitError, match="complex dataset 'vis'"):
        OutriggerN2Reader().probe(str(path))


def test_n2_reader_requires_time_index_to_match_visibility_axis(tmp_path):
    path = tmp_path / "n2-time-mismatch.h5"
    _write_n2(path, n_times=2, index_times=1)
    with pytest.raises(UnreadableUnitError, match="length 2.*time axis"):
        OutriggerN2Reader().probe(str(path))


def test_baseband_inventory_marks_frame_count_as_estimate():
    instrument = load_instrument("chime")
    bytes_per_frame = instrument.nfft * instrument.n_feeds
    row = {"freq_id": 844, "size_bytes": 3 * bytes_per_frame + 123}
    reader = ChimeBasebandReader()
    reader.annotate_row(row, instrument)
    assert reader.survey_schema == 2
    assert row["n_frames_estimate"] == 3
    assert "n_frames" not in row


@pytest.mark.parametrize(
    "band",
    [
        "{f0_mhz: true, bandwidth_mhz: 1, n_channels: 1}",
        "{f0_mhz: 1, bandwidth_mhz: true, n_channels: 1}",
    ],
)
def test_instrument_float_geometry_rejects_booleans(tmp_path, band):
    (tmp_path / "bad.yaml").write_text(
        f"name: bad\nband: {band}\nnyquist_zone: 1\n")
    with pytest.raises(ValueError, match="must be"):
        load_instrument("bad", directory=str(tmp_path))
