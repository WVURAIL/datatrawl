# Adding a telescope

A telescope configuration tells `datatrawl` how to interpret an instrument's data. It
defines the frequency geometry, Nyquist zone, feed count, FFT size, default reader, and
default Datatrail scopes. We keep these values in YAML because adding an instrument should
usually require configuration, not new Python code.

## 1. Copy a YAML

Start with the configuration that is closest to the new instrument. Use
`src/datatrawl/instruments/chime.yaml` for CHIME channelization, or use an outrigger
configuration (`kko`, `gbo`, or `hco`) when the new instrument shares one of those
setups.

```yaml
name: myscope

band:
  f0_mhz: 800.0          # sky frequency at freq_id 0
  bandwidth_mhz: 400.0   # total band; channel spacing = bandwidth / n_channels
  n_channels: 1024       # => fs = 400e6 / 1024 = 390625 Hz per freq_id
  descending: true       # true if freq_id rises as frequency DECREASES

nyquist_zone: 2          # 1 = low-to-high baseband; 2 = high-to-low/inverted baseband
n_feeds: 2048            # feeds combined incoherently (square-then-average)
nfft: 16384              # analysis FFT length per frame

scopes:                  # CADC source only -- the datatrail baseband scope(s) this
  - myscope.event.baseband.raw   # station registers, used as the default survey
                         # scope when --scope is omitted. Drop for local-only use.

reader: chime-baseband   # canonical reader for this telescope's files; scan uses
                         # it when --reader is omitted. CHIME-compatible instruments
                         # reuse it; a new file format means a new reader.
```

The `band` block defines the mapping between `freq_id` and sky frequency. The
`freq_of_freq_id` and `freq_id_of_freq` methods use this block directly. The Nyquist zone
then sets the spectral direction within one coarse `freq_id`:

- `nyquist_zone: 1` means baseband frequency increases in normal low-to-high order.
- `nyquist_zone: 2` means the baseband frequency axis is inverted.

All CHIME-family telescope YAML files currently shipped in this repository use the second
Nyquist zone. We store this value with the telescope because it is an instrument property,
not a parameter to estimate during each run.

If the new telescope shares an existing channelization, use `extends`. For example, the
CHIME outriggers extend `chime`. The child YAML then needs only the values that differ,
usually its name, feed count, and `scopes`. It inherits the remaining geometry, and any
value in the child replaces the inherited value.

Local data do not require Datatrail scopes. For `--source local`, omit `scopes` and provide
the band, `nyquist_zone`, feed count, FFT length, and reader.

## 2. Check readiness

```bash
datatrawl list telescopes
```

- **`ready`** -- the geometry, Nyquist zone, and default baseband `scopes` are set.
- **`geometry-only`** -- the geometry and Nyquist zone are set, but `scopes` is empty. The
  configuration works with `--source local` and with `cadc-datatrail` when `--scope` is
  supplied explicitly.
- **`stub`** -- the Nyquist zone is not set, so the configuration is not ready for a run.

After the telescope appears in the list, check it in a complete pipeline:

```bash
datatrawl doctor --telescope myscope --source local \
                 --source-root /path/to/files \
                 --reader chime-baseband --analyzer spectrum
```

The `chime-baseband` reader expects a `baseband` dataset, unsigned 8-bit 4+4-bit packed
samples, and a `freq` attribute in MHz. If the files use another format, add a reader as
described in [`ADDING_A_READER.md`](ADDING_A_READER.md). The implementation in
`src/datatrawl/plugins/readers/chime_baseband.py` is a complete reference. Readers use the
same `--plugin`, `DATATRAWL_PLUGINS`, and entry-point loading paths documented for
analyzers in [`ADDING_AN_ANALYZER.md`](ADDING_AN_ANALYZER.md).
