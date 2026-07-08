# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The `cadc-datatrail` source now drives Datatrail through the CLI's
  machine-readable mode (`datatrail ls/ps --json`, added upstream in
  datatrail-cli 0.11.0, CHIMEFRB/datatrail-cli#160) instead of importing the
  internal `dtcli.src.functions` module. This resolves the `UPSTREAM NOTE` in
  `_datatrail.py` and closes the internal-dependency exception documented in
  the 1.0.0 release notes. Every adapter method keeps its signature and its
  outage-vs-empty contract; the child runs as `sys.executable -m dtcli.cli`
  (no PATH dependence) with a hard per-call timeout
  (`DATATRAWL_DATATRAIL_TIMEOUT`, default 300 s). The `survey` extra now
  requires `datatrail-cli>=0.11.0` (previously `>=0.10.3,<0.11`). One
  tightening: partial server degradations the internal API surfaced as
  malformed shapes now read as outages (retried), never as "no files".

- The repository graphics are now TikZ-sourced: the four `assets/*.tex`
  (architecture, banner, logo, social card) are the sources of truth ---
  wordmarks and taglines are editable text, and the trawl-net mark lives
  once in `assets/trawlmark.tikz` as a shared pic. `make diagram`
  regenerates the committed SVGs
  (latexmk + pdftocairo, optional scour), and `make docs` builds the data
  sheet and user guide with the verified TeX toolchain documented in the
  README.

## [1.0.0] - 2026-07-03

First stable release. The engine is functionally identical to v0.2.0; this
release attaches the stability contract: the public surface -- the CLI, the
analyzer / reader / source / instrument plugin contracts, and the product and
inventory formats -- now changes only with a major version. The documented
exception is the `dtcli.src.functions` internal-module dependency inside the
`cadc-datatrail` source, which is upstream's surface rather than datatrawl's;
its planned replacement is the `UPSTREAM NOTE` in `_datatrail.py`.

### Changed

- DS001/UG001 rolled to v1.1 (files renamed to match), documenting engine
  v0.2.0: recon (`--scopes-only` with `--match`/`--expand`/`--name`,
  telescope-scoped), the event selection grammar, the reader-owned archive
  file shape (`survey --reader`), `DATATRAIL.files()`, and the per-event
  fan-out / companion side-load patterns. `docs/DATATRAIL_BOUNDARY.md` now
  cross-references the datatrawl mechanisms that consume each side of the
  datatrail hierarchy (recon `--expand` for the `ls` view, `files()` for the
  `ps -s` view).
- README restructured from an outside-user review: the action path (install ->
  verify -> worked example -> local files -> commands) now precedes
  scope/non-goals, a contents line was added, "storage-safe" and "resumable"
  are defined at first use, and the intended audience is stated. The
  duplicated minimal-analyzer example, the Datatrail primer, and the
  upstream-note blockquote were cut to teasers pointing at their canonical
  homes (`docs/ADDING_AN_ANALYZER.md`, `docs/DATATRAIL_BOUNDARY.md`, the
  `UPSTREAM NOTE` in `_datatrail.py`).
- `docs/ADDING_AN_ANALYZER.md`: new "Run parameters (`--set`)" section
  documenting the `--set key=value` -> `ctx.options` path: value typing rules,
  the shared namespace with engine-resolved keys, and when a `--set` option is
  a resume parameter.
- The worked external analyzer moved to its own template repository,
  [`WVURAIL/datatrawl-analyzer-template`](https://github.com/WVURAIL/datatrawl-analyzer-template):
  an installable "Use this template" project that ships `freq_id-peak` behind a
  `datatrawl.plugins` entry point, pinned to `datatrawl v0.2.0`, with a smoke
  suite that runs the real engine on synthetic data -- the packaging and
  entry-point path an in-repo example file could never demonstrate.
  `examples/external_analyzer.py` became `tests/external_analyzer_fixture.py`
  (the plugin-discovery tests still need an out-of-tree analyzer file), the
  README "Quick path" now leads with the template, and a new `template-smoke`
  CI job runs the template's suite against the datatrawl checkout under test,
  so a datatrawl change that breaks the template fails on the datatrawl side.
- DS001/UG001 rolled to v1.2: version designation for the 1.0.0 tag; no
  engine changes documented.

## [0.2.0] - 2026-07-03

### Added

- Event-scoped selection, shared across sources: `--select events:<id>[,...]`
  and the `{"events": [...], "freq_ids": ...}` dict form a `plan_runs` returns
  (grammar in `plugins/sources/_selection.py`; filters are ANDed and exact).
  Local source parses the event from filenames (`--source-event-regex`).
- Per-event fan-out and auxiliary-input (gains/flags/companions) patterns
  documented in `docs/ADDING_AN_ANALYZER.md`, with a worked offline join in
  `examples/match_inventories.py`.
- The worked per-event example reports a per-product summary (event,
  companion, lag, mean power) on the engine's done line.
- `examples/per_event_companions.py`: runnable reference for the per-event +
  companion pattern (plan from the companion table, side-load in `begin()`,
  companion identity stamped and resume-validated), driven end to end through
  the CLI by `tests/test_per_event_scan.py` -- which also pins the join
  example against the real gain-acquisition cadence that motivated it.
- `survey --reader`: the reader whose archive file shape drives the survey,
  enabling inventories of non-baseband products via an external shape reader
  (`docs/ADDING_A_READER.md`).
- "Scope and non-goals" section in the README.
- Recon `--expand` (`survey --scopes-only`): opens each kept dataset one
  level and writes its children to `scopes.jsonl` (rows gain a `parent`
  field), so a container hit like `complex_gains` yields its timestamped
  acquisitions, each directly resolvable with `datatrail ps`. Childless
  containers keep their own row. The `Datatrail` adapter grew `children()`
  (the raw list `events_in_dataset` already extracted from).

### Added (recon robustness, from the first live run)

- Recon `--name`: labels the map `data/scopes-<name>.jsonl` so successive
  recons (gains, n2) don't overwrite each other; default `scopes.jsonl`
  unchanged.
- Outages are never emptiness: recon now uses checked listings (adapter grew
  `list_scopes_checked` / `list_datasets_checked` / `children_checked`), so a
  scope or container datatrail could not answer for is named in-walk and in a
  closing `[warn] the map is INCOMPLETE` block instead of silently missing
  from a clean-looking map. A total scope-listing failure is fatal with an
  actionable message.
- dtcli log leak fixed: datatrail-cli re-arms its own logger level inside
  every call (`set_log_level` under `quiet=True` sets ERROR), so its
  'Service not responding.' records defeated the level-based mute. The mute
  now blocks propagation and detached handlers, which dtcli never touches.

### Added (companion resolution)

- `Datatrail.files(scope, dataset)` -- the programmatic `datatrail ps -s`:
  (common_path, names, ok) with the same prefixing and outage-vs-empty
  contract as `common_path()`, normalization replicating dtcli's own. `ps`
  joined `_REQUIRED_FUNCS`, so doctor validates it up front. The adapter is
  re-exported from `datatrawl.plugins.sources` as the sanctioned import
  path. Documented as the lazy per-event resolution pattern for day-keyed
  companion archives (gains) in `docs/ADDING_AN_ANALYZER.md`, where the
  offline join dissolves into the recon map plus one `files()` call per
  event.

### Changed (survey CLI)

- `--telescope` on `survey` now narrows recon (`--scopes-only`) to that
  telescope's scopes -- selected from datatrail's LIVE namespace by first
  component, deliberately not from the YAML `scopes:` list, which declares
  only what the event survey walks (the gains that motivated this live in a
  scope no YAML declares). It is now optional: omitting it walks every scope
  datatrail can see (zero-knowledge discovery); the event survey still
  requires it, with an actionable error. Explicit `--scope` always wins, and
  a telescope matching zero scopes is a loud error naming the escape hatch,
  never a silent empty map.

### Fixed

- Malformed freq_id selections (`'foo'`, `'506-844-900'`, a bad list element,
  an `events:` string in the `freq_ids` slot) now fail as actionable
  `SystemExit`s naming the grammar, instead of `int()` tracebacks. A reversed
  range (`'844-506'`), which used to parse to an empty set and therefore
  select *everything*, is now a loud error.
- Recon's closing message no longer tells the user to re-run survey against
  a non-event container (which survey's event walk cannot see); it now names
  the correct next step for each case (`--expand` / a shape reader).

### Changed

- The archive file shape (which files one event contributes, and their names)
  moved from the CADC source onto the reader (`Reader.survey_files` /
  `Reader.annotate_row`), fulfilling the parked step-2 design note. Inventory
  rows are now self-describing (each records its file `name`), so enumerate
  stages exactly what survey verified; rows from older inventories still
  reconstruct the baseband naming, and survey without an explicit reader
  falls back to the baseband shape unchanged.

## [0.1.0] - 2026-06-26

### Added

- Storage-safe, resumable file-by-file execution with atomic checkpoints.
- Pluggable instruments, sources, readers, and analyzers.
- Datatrail/CADC inventory surveys and local-directory scans.
- CHIME/outrigger baseband reader and averaged spectrum analyzer.
- External plugin loading by file, module, environment variable, or entry point.
- Quarantine handling, bounded scratch staging, preflight checks, and offline tests.

[Unreleased]: https://github.com/WVURAIL/datatrawl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/WVURAIL/datatrawl/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/WVURAIL/datatrawl/releases/tag/v0.1.0
