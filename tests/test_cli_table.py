from datatrawl.cli import _table


def test_table_wraps_to_terminal_width(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")

    rendered = _table(
        ["name", "status", "instruments", "summary"],
        [["cadc-datatrail", "ready", "chime", "x" * 120]],
    )

    assert max(map(len, rendered.splitlines())) <= 80
    assert rendered.count("x") == 120


def test_table_keeps_headers_and_empty_cells(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")

    rendered = _table(["name", "summary"], [["source", ""]])

    assert rendered.splitlines()[0].startswith("name")
    assert rendered.splitlines()[-1].startswith("source")
