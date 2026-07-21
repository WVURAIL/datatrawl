'''
Outrigger HDF5 gains reader for GBO, HCO, KKO: Parses the filename for date/time, calibration source, weighted/unweighted,and yields the gains array.

Gains hdf5 file format:
    gain               (n_freq, n_input) complex64 -- gain solution
    eval               (n_freq, 2)       complex64 -- eigenvalues
    eval_rms           (n_freq,)         float32   -- eigenvalue RMS
    weight             (n_freq, n_input) float32   -- quality weight
    index_map/freq     (n_freq,)   compound (center_mhz: f8, width_mhz: f8)
    index_map/input    (n_input,)  compound (index: i, serial: bytes)
'''

import re
from datetime import datetime, timezone

import h5py as h5

from datatrawl.interfaces import Reader, PluginInfo, RunContext
from datatrawl.registry import reader as register_reader

# Naming-format info stored here: date/time, calibration target, noise weighted
_FNAME_RE = re.compile(
    r'gain_(?P<ts>\d{8}T\d{6}\.\d+)Z_(?P<cal>[a-z0-9]+)(?P<nw>_noise_weighted)?\.h5$'
)


@register_reader
class OutriggerGainsReader(Reader):
    info = PluginInfo(
        name = 'outrigger-gains',
        kind = 'reader',
        instruments = ("kko", "gbo", "hco"),
        summary = 'Outrigger complex gain files (HDF5).',
    )

    @staticmethod
    def parse_filename(name):
        '''
        Uses the standard gains naming format to find the UTC date/time,
        calibrating source, and whether the gains are noise weighted.
        '''
        m = _FNAME_RE.search(name)
        if not m:
            raise ValueError(f'Not a recognized outrigger gains filename: {name}')
        dt = datetime.strptime(m.group('ts'), '%Y%m%dT%H%M%S.%f')
        return {
            'timestamp': dt.replace(tzinfo = timezone.utc),
            'calibrator': m.group('cal'),
            'noise_weighted': m.group('nw') is not None,
        }

    def probe(self, path):
        '''
        Metadata: filename info, gains array shape

        '''
        meta = self.parse_filename(path.rsplit('/', 1)[-1])
        with h5.File(path, 'r') as f:
            if 'gain' not in f:
                raise KeyError(
                    f"Expected dataset 'gain' not found in {path}; "
                    f'available keys: {list(f.keys())}'
                )
            meta['shape'] = f['gain'].shape
        return meta

    def iter_arrays(self, path, ctx: RunContext):
        '''
        Yields gains array as one item, does not chunk
        '''
        with h5.File(path, 'r') as f:
            yield f['gain'][:]
