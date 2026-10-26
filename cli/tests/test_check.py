import copy
import io
import json
import zipfile

import pytest

from margent import check, cli, limits

L = limits.DEFAULTS


def small(**formats) -> dict:
    """The defaults with some caps lowered: {"csv": {"rows": 2}, ...} or upload={"file_bytes": 10}."""
    lim = copy.deepcopy(L)
    for kind, caps in formats.items():
        (lim if kind in ("upload", "markdown") else lim["tables"]["formats"])[kind].update(caps)
    return lim


def run(tmp_path, files: dict[str, bytes], lim=L):
    for name, data in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    return {r.path: r for r in check.check(tmp_path, sorted(files), lim)}


def test_csv_like_the_server(tmp_path):
    res = run(
        tmp_path,
        {"a.csv": b'id,x\n1,a\n\n2,"b\nc"\n', "b.tsv": b"a\tb\n1\t2\n", "c.csv": "x\nZürich\n".encode("cp1252")},
    )
    assert (res["a.csv"].status, res["a.csv"].message) == ("ok", "2 rows · 2 columns")
    assert res["b.tsv"].message == "1 row · 2 columns"
    assert res["c.csv"].message.endswith("(not UTF-8: read as Windows-1252)")


@pytest.mark.parametrize(
    ("data", "caps", "why"),
    [
        (b"h\n1\n2\n3\n", {"rows": 2}, "over 2 rows"),
        (b"a,b,c\n1,2,3\n", {"columns": 2}, "over 2 columns"),
        (b"h\n" + b"," * 11 + b"\n", {"line_delimiters": 10}, "line 2 has over 10 delimiters"),
    ],
)
def test_csv_over_the_caps(tmp_path, data, caps, why):
    r = run(tmp_path, {"t.csv": data}, small(csv=caps))["t.csv"]
    assert r.status == "table" and why in r.message and "a download, not a table" in r.message


def test_a_cell_over_the_limit(tmp_path):
    lim = copy.deepcopy(L)
    lim["tables"]["cell_bytes"] = 100
    assert "a cell over 100 bytes" in run(tmp_path, {"t.csv": b"a\n" + b"x" * 200 + b"\n"}, lim)["t.csv"].message


def test_ndjson(tmp_path):
    res = run(
        tmp_path, {"a.ndjson": b'{"a": 1}\n\n{"b": [1]}\n', "b.jsonl": b'{"a": 1}\n[1]\n', "c.ndjson": b"{nope\n"}
    )
    assert (res["a.ndjson"].status, res["a.ndjson"].message) == ("ok", "2 rows · 2 columns")
    assert "line 2 is not a JSON object" in res["b.jsonl"].message
    assert "line 1 is not JSON" in res["c.ndjson"].message
    over = run(tmp_path, {"d.ndjson": b'{"a": "xxxx"}\n' * 3}, small(ndjson={"text_bytes": 10}))["d.ndjson"]
    assert over.status == "table" and "of values" in over.message


def workbook(dimension: str | None, sheets=1, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        for i in range(1, sheets + 1):
            dim = f'<dimension ref="{dimension}"/>' if dimension else ""
            z.writestr(f"xl/worksheets/sheet{i}.xml", f"<worksheet>{dim}<sheetData/></worksheet>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def test_xlsx_from_its_directory_and_dimension(tmp_path):
    res = run(
        tmp_path,
        {
            "a.xlsx": workbook("B2:F2001", sheets=2),
            "b.xlsx": workbook(None),
            "c.xlsx": b"not a zip",
            "d.xlsx": workbook("A1:C3", extra={"big.bin": b"\0" * 2000}),
        },
        small(xlsx={"unzipped_bytes": 1500}),
    )
    assert res["a.xlsx"].message == "first sheet up to 2,001 rows · 6 columns; only the first of 2 sheets is shown"
    assert res["b.xlsx"].status == "partial"
    assert res["c.xlsx"].status == "table" and "not an Excel workbook" in res["c.xlsx"].message
    assert res["d.xlsx"].status == "table" and "unpacked" in res["d.xlsx"].message
    tall = run(tmp_path, {"e.xlsx": workbook("A1:A12")}, small(xlsx={"rows": 10}))["e.xlsx"]
    assert tall.status == "table" and "12 rows" in tall.message


def test_parquet_and_arrow_without_pyarrow(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "_pyarrow", lambda: None)
    footer = (100).to_bytes(4, "little")
    res = run(
        tmp_path,
        {
            "a.parquet": b"PAR1" + b"\0" * 200 + footer + b"PAR1",
            "b.parquet": b"PAR1" + b"\0" * 20 + (2**30).to_bytes(4, "little") + b"PAR1",
            "c.parquet": b"nope",
            "d.arrow": b"ARROW1" + b"\0" * 20 + b"ARROW1",
            "e.feather": b"FEA1" + b"\0" * 20,
        },
    )
    assert res["a.parquet"].status == "partial" and "install pyarrow" in res["a.parquet"].message
    assert "footer is over 1.0 MB" in res["b.parquet"].message
    assert res["c.parquet"].status == "table"
    assert res["d.arrow"].status == "partial"
    assert res["e.feather"].status == "table" and "Feather v1" in res["e.feather"].message


def test_upload_refusals_and_large_markdown(tmp_path):
    lim = small(upload={"file_bytes": 50, "total_bytes": 80}, markdown={"source_bytes": 10})
    res = run(tmp_path, {"big.bin": b"x" * 60, "a.md": b"# " + b"x" * 20, "deep/" * 3 + "f": b"x"}, lim)
    assert res["big.bin"].status == "upload" and "the upload is refused" in res["big.bin"].message
    assert res["a.md"].status == "table" and "shown as a plain file" in res["a.md"].message
    assert res["."].status == "upload" and "in all" in res["."].message
    assert "deep/deep/deep/f" not in res  # fine: nothing to say


def test_report_and_exit_code(tmp_path):
    out = io.StringIO()
    ok = check.check(tmp_path, [], L)
    assert check.report(ok, "built-in limits", out) == 0 and "No table files" in out.getvalue()
    bad = list(run(tmp_path, {"t.csv": b"h\n1\n2\n3\n"}, small(csv={"rows": 2})).values())
    out = io.StringIO()
    assert check.report(bad, "x's limits", out) == 1
    assert "not a table  t.csv: over 2 rows" in out.getvalue()


def test_fetch_uses_the_server_and_falls_back(monkeypatch):
    assert limits.fetch(None) == (L, "built-in limits (no server configured)")

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    served = copy.deepcopy(L)
    served["tables"]["formats"]["csv"]["rows"] = 7
    seen = {}

    def urlopen(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        return Resp(json.dumps(served).encode())

    monkeypatch.setattr(limits.urllib.request, "urlopen", urlopen)
    lim, source = limits.fetch("https://m.example")
    assert lim["tables"]["formats"]["csv"]["rows"] == 7 and source == "https://m.example's limits"
    assert seen["auth"] is None  # no token goes out for limits
    monkeypatch.setattr(limits.urllib.request, "urlopen", lambda req, timeout: Resp(b'{"version": 2}'))
    assert limits.fetch("https://m.example")[0] is L

    def down(req, timeout):
        raise OSError("refused")

    monkeypatch.setattr(limits.urllib.request, "urlopen", down)
    lim, source = limits.fetch("https://m.example")
    assert lim is L and "not reachable" in source


def test_check_command_outside_git(tmp_path, monkeypatch, capsys):
    (tmp_path / "d").mkdir()
    (tmp_path / "d/t.csv").write_text("a\n1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MARGENT_SERVER", raising=False)
    assert cli.main(["check", "d"]) == 0
    assert "ok  t.csv: 1 row · 1 column" in capsys.readouterr().out
