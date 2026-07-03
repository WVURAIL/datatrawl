# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Event-scoped selection, shared across sources: `--select events:<id>[,...]`
  and the `{"events": [...], "freq_ids": ...}` dict form a `plan_runs` returns
  (grammar in `plugins/sources/_selection.py`; filters are ANDed and exact).
  Local source parses the event from filenames (`--source-event-regex`).
- Per-event fan-out and auxiliary-input (gains/flags/companions) patterns
  documented in `docs/ADDING_AN_ANALYZER.md`, with a worked offline join in
  `examples/match_inventories.py`.
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

[Unreleased]: https://github.com/WVURAIL/datatrawl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/WVURAIL/datatrawl/releases/tag/v0.1.0
