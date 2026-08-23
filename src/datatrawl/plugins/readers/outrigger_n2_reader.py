'''
Outrigger N-squared reader: Finds time range of n2 file and yields
visibilities array.

Folder naming:
    <YYYYMMDDTHHMMSS>Z_gbo[stack|rfi|cal|subband]_corr

Individual file layout (confirmed from a real file):
    vis                (n_freq, n_prod, n_time) complex64 
    erms               (n_freq, n_time)         float32  
    eval               (n_freq, 4, n_time)      float32  
    evec               (n_freq, 4, n_input, n_time) complex64 
    gain               (n_freq, n_input, n_time) complex64
    flags/vis_weight   (n_freq, n_prod, n_time)  float32  
    flags/frac_lost    (n_freq, n_time)          float32
    flags/frac_rfi     (n_freq, n_time)          float32
    flags/inputs       (n_input, n_time)         float32
    flags/dataset_id   (n_freq, n_time)          bytes
    index_map/freq     (n_freq,)  compound
    index_map/input    (n_input,) compound
    index_map/prod     (n_prod,)  compound
    index_map/ev       (4,)       uint32
    index_map/time     (n_time,)  compound    <---- (contains 'ctime')
'''

import re

try:
    # Importing hdf5plugin registers the HDF5 compression filters the N-squared
    # files are written with. Optional (`pip install "datatrawl[outriggers]"`);
    # preflight reports its absence so `doctor` can flag it before a run.
    import hdf5plugin  # noqa: F401
except ImportError:
    hdf5plugin = None
import h5py as h5

from datatrawl.interfaces import (Reader, PluginInfo, RunContext,
                                  STREAM_VISIBILITY_CHUNK)
from datatrawl.registry import reader as register_reader
from ._unreadable import unreadable_file

_FOLDER_RE = re.compile(
    r'^(?P<ts>\d{8}T\d{6})Z_(?P<site>gbo|hco|kko)(?P<variant>stack|rfi|cal|subband)?_corr$'
)

# Fixed CHIME channelization / baseline-count assumption
_EXPECTED_N_FREQ = 1024

# Default chunk size along the frequency axis for iter_arrays()
_DEFAULT_FREQ_CHUNK = 128


def _visibility_dataset(handle, path):
    if 'vis' not in handle:
        raise KeyError(
            f"Expected dataset 'vis' not found in {path}; "
            f"available keys: {list(handle.keys())}"
        )
    vis = handle['vis']
    shape = vis.shape
    if vis.ndim != 3:
        raise ValueError(
            f"Expected 3-D dataset 'vis' in {path}, got shape {shape}")
    if vis.dtype.kind != 'c':
        raise ValueError(
            f"Expected complex dataset 'vis' in {path}, got dtype {vis.dtype}")
    if shape[0] != _EXPECTED_N_FREQ:
        raise ValueError(
            f"Expected {_EXPECTED_N_FREQ} freq channels, got "
            f"{shape[0]} in {path} -- fixed-channelization "
            f"assumption doesn't hold for this file. (Confirmed "
            f"to hold for both gbo and kko directly -- this "
            f"assumption travels across sites even though input/"
            f"product count does not.)"
        )
    return vis


@register_reader
class OutriggerN2Reader(Reader):
    survey_schema = 1

    info = PluginInfo(
        name = 'outrigger-n2',
        kind = 'reader',
	instruments = ("gbo", "hco", "kko"),
        summary = 'Read outrigger N-squared (visibility correlation) files (HDF5).',
        stream_kind = STREAM_VISIBILITY_CHUNK,
    )

    def preflight(self, ctx: RunContext) -> tuple[bool, list[str]]:
        if hdf5plugin is None:
            return False, ["hdf5plugin is not installed; N-squared files use "
                           "its HDF5 compression filters: "
                           "pip install 'datatrawl[outriggers]'"]
        return True, []

    @staticmethod
    def parse_folder_name(name):
        '''
        Parses folder info (site, starting date/time of folder, variant vs plain n2 data)
        '''
        m = _FOLDER_RE.match(name)
        if not m:
            raise ValueError(f'Not a recognized N2 commissioning folder name: {name}')
        from datetime import datetime, timezone
        dt = datetime.strptime(m.group('ts'), '%Y%m%dT%H%M%S')
        return {
            'timestamp': dt.replace(tzinfo = timezone.utc),
            'site': m.group('site'),
            'variant': m.group('variant') or 'plain',
        }

    def probe(self, path):
        '''
        Finds shape of visibilities array and time range of file
        '''
        with unreadable_file():
            with h5.File(path, 'r') as f:
                shape = _visibility_dataset(f, path).shape
                ctimes = f['index_map/time']['ctime']
                if not ctimes.size:
                    raise ValueError(
                        f"Expected non-empty 'index_map/time' in {path}")
                if ctimes.ndim != 1 or ctimes.shape[0] != shape[2]:
                    raise ValueError(
                        f"Expected 'index_map/time' length {shape[2]} to match "
                        f"the vis time axis in {path}, got shape {ctimes.shape}")
                return {
                    'shape': shape,
                    'ctime_min': float(ctimes.min()),
                    'ctime_max': float(ctimes.max()),
                }

    def iter_arrays(self, path, ctx: RunContext, freq_chunk = _DEFAULT_FREQ_CHUNK):
        '''
        Yields visibility array in chunks
        '''
        if not isinstance(freq_chunk, int) or isinstance(freq_chunk, bool):
            raise TypeError("freq_chunk must be an integer")
        if freq_chunk < 1:
            raise ValueError("freq_chunk must be positive")
        with unreadable_file():
            with h5.File(path, 'r') as f:
                vis = _visibility_dataset(f, path)
                n_freq = vis.shape[0]
                for start in range(0, n_freq, freq_chunk):
                    end = min(start + freq_chunk, n_freq)
                    yield {
                        'vis': vis[start:end],
                        'freq_start': start,
                        'freq_end': end,
                    }
