"""
Reader: CHIME / outrigger baseband HDF5 (offset-binary 4+4-bit).

The concrete reader the bundled example runs on. It owns the on-disk format
knowledge via `_baseband_format` and yields the [nfft, n_feeds] complex64 frames
an analyzer consumes:

    dataset  h["baseband"]   uint8 [n_time, n_feeds], offset-binary 4+4-bit
    attr     h.attrs["freq"] channel-centre frequency in MHz

CHIME and its outriggers (KKO/GBO/HCO) share this product, so this one reader
serves them all. A different file format needs a different reader, not a change
here -- see docs/ADDING_A_READER.md (this file is a worked example).
"""
from __future__ import annotations

from typing import Iterable, Iterator, Mapping

from ...interfaces import Reader, RunContext, PluginInfo, READY
from ...registry import reader as _register_reader
from . import _baseband_format as fmt


def baseband_filename(event, freq_id) -> str:
    """The archive filename of one baseband unit. THE naming definition --
    survey (via survey_files) and the legacy-inventory fallback in the CADC
    source both use this, so the shape cannot drift between them."""
    return f"baseband_{event}_{freq_id}.h5"


@_register_reader
class ChimeBasebandReader(Reader):
    info = PluginInfo(
        name="chime-baseband",
        kind="reader",
        summary="CHIME/outrigger baseband HDF5 (offset-binary 4+4-bit, "
                "dataset 'baseband', attr 'freq' in MHz).",
        status=READY,
        instruments=("chime", "kko", "gbo", "hco"),
        requires=("h5py",),
        notes="Yields [nfft, n_feeds] complex64 frames. Shared by CHIME and all "
              "CHIME-compatible outriggers.",
    )

    def probe(self, path: str) -> Mapping[str, object]:
        f_center_hz = fmt.channel_center_hz(path)
        return {"f_center_hz": f_center_hz,
                "f_center_mhz": f_center_hz / 1e6,
                "fs_hz": fmt.FS,
                "nfft": fmt.NFFT}

    def iter_arrays(self, path: str, ctx: RunContext) -> Iterator:
        nfft = int(getattr(ctx.instrument, "nfft", fmt.NFFT) or fmt.NFFT)
        return fmt.iter_frames(path, nfft=nfft)

    # -- archive file shape ------------------------------------------------
    # Baseband: one HDF5 per freq_id per event. `selection` is the survey's
    # resolved freq_id list (see CadcDatatrailSource.survey), which is exactly
    # how this product is selected.
    def survey_files(self, event, common_path, selection,
                     ctx: RunContext) -> Iterable[tuple]:
        for ch in (selection or ()):
            yield baseband_filename(event, ch), {"freq_id": int(ch)}

    def annotate_row(self, row: dict, instrument) -> None:
        """Geometry annotation, from the YAML: channel centre + a frame count
        derived from the packed 4+4-bit layout (1 byte per feed per sample)."""
        if instrument is None or row.get("freq_id") is None:
            return
        ch = int(row["freq_id"])
        bytes_per_frame = instrument.nfft * instrument.n_feeds
        row["freq_mhz"] = round(instrument.freq_of_freq_id(ch), 4)
        row["n_frames"] = round(row.get("size_bytes", 0) / bytes_per_frame, 4)
