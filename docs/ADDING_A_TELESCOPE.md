# Adding a telescope

A telescope is just **geometry**: how its baseband freq_ids map to sky frequency, its
Nyquist zone, feed count, FFT size, and default reader. Adding one is usually a single YAML
file, no Python.

## 1. Copy a YAML

Start from `src/datatrawl/instruments/chime.yaml`, or an outrigger config (`kko`, `gbo`,
`hco`) if your instrument shares CHIME's channelization.

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

The band block drives `freq_of_freq_id` / `freq_id_of_freq` (freq_id -> sky
frequency). The Nyquist zone controls the spectral direction inside a coarse freq_id:

- `nyquist_zone: 1` means baseband frequency increases in normal low-to-high order.
- `nyquist_zone: 2` means the baseband frequency axis is inverted.

All current CHIME-family telescope YAMLs in this repository use the second Nyquist zone.
This is a telescope property, not something each user should measure during a run.

If your telescope shares an existing one's channelization (as the CHIME outriggers share
CHIME's), set `extends: chime` and list only what differs -- typically the name, feed
count, and `scopes`. The parent's geometry is inherited and any key you set overrides it.

For local-only use (`--source local`), drop the `scopes` list; the minimum is the band,
`nyquist_zone`, feed count, FFT length, and reader.

## 2. Check readiness

```bash
datatrawl list telescopes
```

- **`ready`** -- geometry + Nyquist zone + a default baseband `scopes` list all set.
- **`geometry-only`** -- geometry + Nyquist zone set but no `scopes`; fully usable with
  `--source local`, and with `cadc-datatrail` if you pass `--scope`.
- **`stub`** -- Nyquist zone not yet set.

Then confirm a concrete run:

```bash
datatrawl doctor --telescope myscope --source local \
                 --source-root /path/to/files \
                 --reader chime-baseband --analyzer spectrum
```

If your files are not CHIME baseband (dataset `baseband`, uint8 4+4-bit, attr `freq` in
MHz), they need a new reader -- see [`ADDING_A_READER.md`](ADDING_A_READER.md)
(`src/datatrawl/plugins/readers/chime_baseband.py` is a worked example). The plugin-loading
mechanics (`--plugin`, `DATATRAWL_PLUGINS`, entry points) are the same as for analyzers in
[`ADDING_AN_ANALYZER.md`](ADDING_AN_ANALYZER.md).
