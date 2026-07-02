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
- `survey --reader`: the reader whose archive file shape drives the survey,
  enabling inventories of non-baseband products via an external shape reader
  (`docs/ADDING_A_READER.md`).
- "Scope and non-goals" section in the README.

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
