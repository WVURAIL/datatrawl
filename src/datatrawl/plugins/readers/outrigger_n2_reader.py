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
import os

import hdf5plugin
import h5py as h5

from datatrawl.interfaces import Reader, PluginInfo, RunContext
from datatrawl.registry import reader as register_reader

_FOLDER_RE = re.compile(
    r'^(?P<ts>\d{8}T\d{6})Z_(?P<site>gbo|hco|kko)(?P<variant>stack|rfi|cal|subband)?_corr$'
)

# Fixed CHIME channelization / baseline-count assumption
_EXPECTED_N_FREQ = 1024

# Default chunk size along the frequency axis for iter_arrays()
_DEFAULT_FREQ_CHUNK = 128


@register_reader
class OutriggerN2Reader(Reader):
    info = PluginInfo(
        name = 'outrigger-n2',
        kind = 'reader',
	instruments = ("gbo", "hco", "kko"),
        summary = 'Read outrigger N-squared (visibility correlation) files (HDF5).',
    )

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
        with h5.File(path, 'r') as f:
            if 'vis' not in f:
                raise KeyError(
                    f"Expected dataset 'vis' not found in {path}; "
                    f"available keys: {list(f.keys())}"
                )
            shape = f['vis'].shape
            if shape[0] != _EXPECTED_N_FREQ:
                raise ValueError(
                    f"Expected {_EXPECTED_N_FREQ} freq channels, got "
                    f"{shape[0]} in {path} -- fixed-channelization "
                    f"assumption doesn't hold for this file. (Confirmed "
                    f"to hold for both gbo and kko directly -- this "
                    f"assumption travels across sites even though input/"
                    f"product count does not.)"
                )

            # Product-count check is BOTH site and variant-dependent
            site = variant = None
            try:
                folder_name = os.path.basename(os.path.dirname(path))
                parsed = OutriggerN2Reader.parse_folder_name(folder_name)
                site, variant = parsed['site'], parsed['variant']
            except (ValueError, OSError):
                pass  # unrecognized folder name -- skip the product check below

            _expected_n_prod_by_site_variant = {
                ('gbo', 'plain'): 32896,
                ('gbo', 'rfi'): 256,
                ('gbo', 'stack'): 256,
                ('kko', 'plain'): 8256,
                ('hco', 'plain'): 32896,
            }
            expected_n_prod = _expected_n_prod_by_site_variant.get((site, variant))
            if expected_n_prod is not None and shape[1] != expected_n_prod:
                raise ValueError(
                    f"Expected {expected_n_prod} baseline products for "
                    f"{site}/'{variant}', got {shape[1]} in {path} -- "
                    f"the confirmed shape for this site/variant doesn't "
                    f"hold for this file."
                )

            ctimes = f['index_map/time']['ctime']
            return {
                'shape': shape,
                'ctime_min': float(ctimes.min()),
                'ctime_max': float(ctimes.max()),
            }

    def iter_arrays(self, path, ctx: RunContext, freq_chunk = _DEFAULT_FREQ_CHUNK):
        '''
        Yields visibility array in chunks
        '''
        with h5.File(path, 'r') as f:
            vis = f['vis']
            n_freq = vis.shape[0]
            for start in range(0, n_freq, freq_chunk):
                end = min(start + freq_chunk, n_freq)
                yield {
                    'vis': vis[start:end],
                    'freq_start': start,
                    'freq_end': end,
                }
