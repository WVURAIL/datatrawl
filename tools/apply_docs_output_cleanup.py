#!/usr/bin/env python3
"""Apply the datatrawl docs/output cleanup with branch-tolerant edits.

This helper exists because datatrawl has had parallel documentation branches
using different names (`analyzer`/`freq_id` versus `reducer`/`channel`, and
`survey` versus `crawl`). It updates whichever files and phrases exist in the
current checkout, then leaves a normal git diff for review.

Run from the repository root:

    python tools/apply_docs_output_cleanup.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path.cwd()
CHANGED: list[str] = []
SKIPPED: list[str] = []


def read(path: str) -> str | None:
    p = ROOT / path
    if not p.exists():
        SKIPPED.append(f"missing: {path}")
        return None
    return p.read_text()


def write(path: str, text: str, old: str | None) -> None:
    if old is None:
        return
    if text != old:
        (ROOT / path).write_text(text)
        CHANGED.append(path)


def insert_after(text: str, needle: str, addition: str) -> str:
    if addition.strip() in text:
        return text
    i = text.find(needle)
    if i < 0:
        return text
    j = i + len(needle)
    return text[:j] + addition + text[j:]


def append_if_missing(text: str, addition: str) -> str:
    return text if addition.strip() in text else text.rstrip() + "\n\n" + addition.strip() + "\n"


def replace_block(text: str, start: str, end: str, replacement: str) -> str:
    if replacement.strip() in text:
        return text
    i = text.find(start)
    j = text.find(end, i + len(start)) if i >= 0 else -1
    if i < 0 or j < 0:
        return text
    return text[:i] + replacement.rstrip() + "\n\n" + text[j:]


def update_readme() -> None:
    path = "README.md"
    old = read(path)
    if old is None:
        return
    text = old

    # Analyzer/freq_id branch: keep README at workflow level and point to the guide.
    if "### Minimal analyzer shape" in text and "docs/ADDING_AN_ANALYZER.md" in text:
        replacement = """### Analyzer implementation details

An analyzer consumes arrays from a reader and writes a small resumable product.
The README stops at the workflow level so the implementation contract has one
home. For the full analyzer shape, including the minimal class, order-dependence,
fan-out, plugin loading, and resume validation, see
[`docs/ADDING_AN_ANALYZER.md`](docs/ADDING_AN_ANALYZER.md).

For real analyzers, validate every option that changes product meaning in
`resume()`, not only in `begin()`. Typical invariants include `freq_id`, `nfft`,
detector thresholds, window choice, Nyquist zone, frame caps, and calibration
constants.
"""
        text = replace_block(text, "### Minimal analyzer shape", "## Guides", replacement)

    # Add the CLI output guide to either old table or newer bullet-list docs.
    if "docs/CLI_OUTPUT.md" not in text:
        table_line = "| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Long runs, self-healing, quarantine, expired certs, and recovery. |"
        if table_line in text:
            text = text.replace(
                table_line,
                "| [`docs/CLI_OUTPUT.md`](docs/CLI_OUTPUT.md) | Interpret `doctor`, `survey`, `explore`, and `scan` output. |\n" + table_line,
            )
        elif "- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)" in text:
            text = text.replace(
                "- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)",
                "- [`docs/CLI_OUTPUT.md`](docs/CLI_OUTPUT.md) --- interpret `doctor`, inventory, `explore`, and `scan` output.\n- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)",
                1,
            )
        elif "## Documentation" in text:
            text = insert_after(
                text,
                "## Documentation",
                "\n\n- [`docs/CLI_OUTPUT.md`](docs/CLI_OUTPUT.md) --- interpret command output and status lines.",
            )
        else:
            text = append_if_missing(
                text,
                "See [`docs/CLI_OUTPUT.md`](docs/CLI_OUTPUT.md) for command-output status lines and recovery hints.",
            )

    write(path, text, old)


def update_analysis_guides() -> None:
    # Older analyzer guide.
    path = "docs/ADDING_AN_ANALYZER.md"
    old = read(path)
    if old is not None:
        addition = """

## What belongs where

| Component | Responsibility |
|---|---|
| Engine | Stages one unit, calls the reader, calls the analyzer, deletes scratch data, checkpoints, resumes, and quarantines unreadable files. |
| Reader | Converts one staged file into array frames and small per-file metadata. |
| Analyzer | Consumes array frames, records successfully processed units, validates resume invariants, and writes the small science product. |

Keep data-format parsing in the reader and science state in the analyzer. The
engine passes arrays through unchanged and only relies on the analyzer's processed
unit keys for resume.
"""
        text = insert_after(old, "The public and internal plugin type is `analyzer`.\n", addition)
        write(path, text, old)

    # Newer reducer/reader combined guide.
    path = "docs/ADDING_AN_ANALYSIS.md"
    old = read(path)
    if old is not None:
        addition = """

## What belongs where

| Component | Responsibility |
|---|---|
| Engine | Stages one unit, calls the reader, calls the reducer, deletes scratch data, checkpoints, resumes, and quarantines unreadable files. |
| Reader | Converts one staged file into array chunks and small per-file metadata. |
| Reducer | Consumes array chunks, records successfully processed units, validates resume invariants, and writes the small science product. |

Keep data-format parsing in the reader and science state in the reducer. The
engine passes arrays through unchanged and only relies on the reducer's processed
unit keys for resume.
"""
        if "## What belongs where" not in old:
            marker = "Two plugin types carry the science:"
            if marker in old:
                text = insert_after(old, marker, addition)
            else:
                text = append_if_missing(old, addition)
        else:
            text = old
        write(path, text, old)


def update_source_reader_guides() -> None:
    for path, kind in [("docs/ADDING_A_SOURCE.md", "source"), ("docs/ADDING_A_READER.md", "reader")]:
        old = read(path)
        if old is None:
            continue
        text = old
        heading = "## Registering and loading"
        if heading in text and "Use the same external-plugin mechanisms" not in text:
            guide = "ADDING_AN_ANALYZER.md#loading-external-analyzers"
            replacement = f"""## Registering and loading

Use the same external-plugin mechanisms documented for analyzers:

- `--plugin /path/to/my_{kind}.py`;
- `DATATRAWL_PLUGINS=/path/to/my_{kind}.py`;
- a package entry point in `[project.entry-points.\"datatrawl.plugins\"]`.

For import-path details, packaged plugins, and entry-point metadata refreshes, see
[`{guide}`]({guide}).

Once loaded, the {kind} shows up in `datatrawl list` / `doctor` and runs through
the same engine (staging, dedup, quarantine, resume) as a built-in.
"""
            # Replace from heading to EOF because these files end with this section in the old branch.
            text = text[: text.find(heading)] + replacement
        write(path, text, old)


def update_troubleshooting() -> None:
    path = "docs/TROUBLESHOOTING.md"
    old = read(path)
    if old is None:
        return
    text = old
    addition = "For a compact reference to status markers and per-command output, see [`CLI_OUTPUT.md`](CLI_OUTPUT.md)."
    if "CLI_OUTPUT.md" not in text:
        if "## Symptom" in text:
            text = text.replace("## Symptom", addition + "\n\n## Symptom", 1)
        else:
            text = append_if_missing(text, addition)
    write(path, text, old)


def update_cli() -> None:
    path = "src/datatrawl/cli.py"
    old = read(path)
    if old is None:
        return
    text = old

    text = text.replace(
        'help="quarantine ledger path (default "\n                             "results/<tel>/quarantine.jsonl); bad/unreadable "\n                             "files are recorded here and skipped on re-runs")',
        'help="quarantine ledger path (default "\n                             "results/<tel>/quarantine/<source>--<reader>.jsonl); "\n                             "bad/unreadable files are recorded here and skipped "\n                             "on re-runs")',
    )
    text = text.replace(
        'help="quarantine ledger path (default "\n                             "results/<tel>/quarantine.jsonl); bad/unreadable "\n                             "files are recorded here and skipped on re-runs")',
        'help="quarantine ledger path (default "\n                             "results/<tel>/quarantine/<source>--<reader>.jsonl); "\n                             "bad/unreadable files are recorded here and skipped "\n                             "on re-runs")',
    )

    text = text.replace(
        'print(f"[survey] {instrument.name} via {args.source} -> {out_dir}")',
        'print(f"[survey] start: telescope={instrument.name} source={args.source} "\n          f"out={out_dir}")',
    )
    text = text.replace(
        'print(f"[crawl] {instrument.name} via {args.source} -> {out}")',
        'print(f"[crawl] start: telescope={instrument.name} source={args.source} "\n          f"out={out}")',
    )

    # Improve the local explore hint in the analyzer/freq_id branch when the exact block exists.
    old_block = '''            print(f"  datatrawl scan --analyzer <analyzer> --select {sample}")
            print("    (a surveyed inventory stores telescope/source/reader; a local")
            print("     directory has none, so pass --telescope, --source, and --reader)")'''
    new_block = '''            if source_name == "local":
                tel = instrument.name if instrument else "<telescope>"
                print(f"  datatrawl scan --source local --source-root <dir> "
                      f"--telescope {tel} --reader <reader> "
                      f"--analyzer <analyzer> --select {sample}")
                print("    (replace <dir>, <reader>, and <analyzer>; local scans")
                print("     do not have inventory meta to fill those in)")
            else:
                tel = instrument.name if instrument else "<telescope>"
                print(f"  datatrawl scan --source {source_name} --telescope {tel} "
                      f"--reader <reader> --analyzer <analyzer> --select {sample}")
                print("    (if this came from a survey, prefer --name or --inventory")
                print("     so telescope/source/reader come from the inventory meta)")'''
    text = text.replace(old_block, new_block)

    # Newer reducer/channel branch variant.
    text = text.replace(
        'print(f"  datatrawl scan --reducer <reducer> --select {sample}")',
        'print(f"  datatrawl scan --source {source_name} --telescope {instrument.name if instrument else \'<telescope>\'} "\n'
        '      f"--reader <reader> --reducer <reducer> --select {sample}")',
    )

    write(path, text, old)


def update_cadc_source() -> None:
    path = "src/datatrawl/plugins/sources/cadc_datatrail.py"
    old = read(path)
    if old is None:
        return
    text = old

    text = text.replace(
        'print(f"[survey] scopes={list(scopes)} freq_ids={len(freq_ids)} "\n              f"({freq_ids[0]}..{freq_ids[-1]}) -> {inv_path}", flush=True)',
        'print(f"[cadc-datatrail] survey: scopes={list(scopes)} "\n              f"freq_ids={len(freq_ids)} ({freq_ids[0]}..{freq_ids[-1]}) "\n              f"-> {inv_path}", flush=True)',
    )
    text = text.replace(
        'print(f"[crawl] scopes={list(scopes)} channels={len(channels)} "\n              f"({channels[0]}..{channels[-1]}) -> {inv_path}", flush=True)',
        'print(f"[cadc-datatrail] crawl: scopes={list(scopes)} "\n              f"channels={len(channels)} ({channels[0]}..{channels[-1]}) "\n              f"-> {inv_path}", flush=True)',
    )

    long_warn = '''print(
                "[warn] inventory.jsonl is EMPTY (0 rows). Every surveyed event "
                "resolved to zero retrievable files, so nothing was written -- "
                "usually the environment, not the survey. Sanity-check one event: "
                "`datatrail ps <scope> <event> -s` (is a 'Common Path:' line "
                "printed?), then `cadcinfo --cert ~/.ssl/cadcproxy.pem <cadc-uri>` "
                "for one freq_id (NotFound = the bytes aged off storage, or a size "
                "under the 1 MiB floor; pass the cert or the CLI runs anonymously and "
                "reports a misleading 'Unauthorized'). The lowest event IDs are the "
                "likeliest to have aged out of the archive, so a larger "
                "--max-events often starts filling the inventory.", flush=True)'''
    split_warn = '''print(
                "[warn] inventory.jsonl is EMPTY (0 rows). No scan units were written.",
                flush=True)
            print("       Check one event: PAGER=cat datatrail ps <scope> <event> -s",
                  flush=True)
            print("       Then check one expected file with the same cert, e.g.:",
                  flush=True)
            print("       cadcinfo --cert ~/.ssl/cadcproxy.pem <cadc-uri>",
                  flush=True)
            print("       NotFound usually means the bytes aged off storage, the file "
                  "is below the size floor, or the URI is not the expected one.",
                  flush=True)
            print("       If you started with the oldest events, try a larger "
                  "--max-events so newer retrievable events are included.",
                  flush=True)'''
    text = text.replace(long_warn, split_warn)

    # More tolerant fallback for branches where the warning text wrapped differently.
    if "inventory.jsonl is EMPTY" in text and "No scan units were written" not in text:
        text = re.sub(
            r'print\(\s*"\[warn\] inventory\.jsonl is EMPTY \(0 rows\).*?flush=True\)',
            split_warn,
            text,
            count=1,
            flags=re.S,
        )

    write(path, text, old)


def main() -> None:
    update_readme()
    update_analysis_guides()
    update_source_reader_guides()
    update_troubleshooting()
    update_cli()
    update_cadc_source()

    print("Updated files:" if CHANGED else "No files changed.")
    for path in CHANGED:
        print(f"  {path}")
    if SKIPPED:
        print("Skipped:")
        for item in SKIPPED:
            print(f"  {item}")
    print("\nReview with: git diff")


if __name__ == "__main__":
    main()
