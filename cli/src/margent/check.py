"""margent check: will the server show these files as tables, and accept the upload at all?

Compares a folder against the server's limits (margent.limits) before a push. It reads
files the way the server does where the standard library can (CSV, NDJSON, an Excel
workbook's zip directory and sheet size, Parquet and Arrow footers). With pyarrow
installed it also checks Parquet and Arrow rows, columns and types. What only decoding
on the server can tell (a binary file's unpacked text size) is left to the server.
"""

import csv
import json
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

_DELIMITERS = ",;\t|"


@dataclass
class Result:
    path: str
    status: str  # "ok", "partial" (only partly checkable here), "table" (won't be a table), "upload" (refused)
    message: str


class _Over(Exception):
    pass


def _n(x: int) -> str:
    return f"{x:,}"


def _shape(rows: int, cols: int) -> str:
    return f"{_n(rows)} row{'' if rows == 1 else 's'} · {cols} column{'' if cols == 1 else 's'}"


def _size(b: int) -> str:
    return f"{b / 2**20:.1f} MB" if b >= 2**20 else f"{_n(b)} bytes"


def kind_of(path: str, limits: dict) -> tuple[str, dict] | None:
    lower = path.lower()
    for kind, f in limits["tables"]["formats"].items():
        if any(lower.endswith(e) for e in f["extensions"]):
            return kind, f
    return None


# --- CSV/TSV: the server's own rules (server/builds/tabledata.py) --------------------------


def _encoding(src: Path) -> tuple[str, str]:
    try:
        with src.open(encoding="utf-8-sig", newline="") as f:
            while f.read(1 << 20):
                pass
        return "utf-8-sig", "strict"
    except UnicodeDecodeError:
        return "cp1252", "replace"


def _csv(src: Path, path: str, f: dict, cell_bytes: int) -> str:
    enc = _encoding(src)
    with src.open(encoding=enc[0], errors=enc[1], newline="") as fh:
        sample = fh.read(64 * 1024)
    if path.lower().endswith(".tsv"):
        dialect = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=_DELIMITERS)
        except csv.Error:
            dialect = csv.excel
    old = csv.field_size_limit(cell_bytes)
    rows, width, header = 0, 0, False
    try:
        with src.open(encoding=enc[0], errors=enc[1], newline="") as fh:

            def lines():
                for n, line in enumerate(fh, 1):
                    if line.count(dialect.delimiter) > f["line_delimiters"]:
                        raise _Over(f"line {n} has over {_n(f['line_delimiters'])} delimiters")
                    yield line

            for record in csv.reader(lines(), dialect):
                if not any(c.strip() for c in record):
                    continue
                width = max(width, len(record))
                if header:
                    rows += 1
                header = True
                if width > f["columns"]:
                    raise _Over(f"over {_n(f['columns'])} columns")
                if rows > f["rows"]:
                    raise _Over(f"over {_n(f['rows'])} rows")
    except csv.Error as exc:
        raise _Over(f"a cell over {_size(cell_bytes)}") from exc
    finally:
        csv.field_size_limit(old)
    note = "" if enc[0] == "utf-8-sig" else " (not UTF-8: read as Windows-1252)"
    return f"{_shape(rows, width)}{note}"


# --- NDJSON ------------------------------------------------------------------------------


def _json_text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except RecursionError as exc:  # Python 3.14 parses deeper nesting than it writes back
        raise _Over("a value nested too deep") from exc


def _ndjson(src: Path, f: dict, cell_bytes: int) -> str:
    keys: set[str] = set()
    rows = text = 0
    with src.open("rb") as fh:
        for n, line in enumerate(fh, 1):
            if len(line) > cell_bytes:
                raise _Over(f"line {n} is over {_size(cell_bytes)}")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError) as exc:
                raise _Over(f"line {n} is not JSON (or not UTF-8)") from exc
            if not isinstance(obj, dict):
                raise _Over(f"line {n} is not a JSON object")
            keys.update(obj)
            rows += 1
            text += sum(len((_json_text(v) or "").encode()) for v in obj.values())
            if len(keys) > f["columns"]:
                raise _Over(f"over {_n(f['columns'])} columns (distinct keys)")
            if rows > f["rows"]:
                raise _Over(f"over {_n(f['rows'])} rows")
            if text > f["text_bytes"]:
                raise _Over(f"over {_size(f['text_bytes'])} of values")
    return _shape(rows, len(keys))


# --- binary formats: footers and directories here, decoding on the server ------------------


def _pyarrow():
    try:
        import pyarrow  # noqa: F401
        import pyarrow.ipc
        import pyarrow.parquet

        return pyarrow
    except ImportError:
        return None


def _plain(t, pa) -> bool:
    """Types the server shows as text (no lists, structs, maps or binary)."""
    ty = pa.types
    if ty.is_dictionary(t):
        return _plain(t.value_type, pa)
    return bool(
        ty.is_integer(t) or ty.is_floating(t) or ty.is_decimal(t) or ty.is_boolean(t) or ty.is_null(t)
        or ty.is_string(t) or ty.is_large_string(t) or ty.is_temporal(t)
    )  # fmt: skip


def _parquet(src: Path, f: dict) -> tuple[str, str]:
    size = src.stat().st_size
    with src.open("rb") as fh:
        head = fh.read(4)
        fh.seek(max(0, size - 8))
        tail = fh.read(8)
    if size < 12 or head != b"PAR1" or tail[4:] != b"PAR1":
        raise _Over("not a Parquet file")
    footer = struct.unpack("<I", tail[:4])[0]
    if footer > f["footer_bytes"]:
        raise _Over(f"its footer is over {_size(f['footer_bytes'])}")
    pa = _pyarrow()
    if pa is None:
        return "partial", "footer ok; install pyarrow to check rows, columns and types here"
    md = pa.parquet.read_metadata(src)
    schema = pa.parquet.read_schema(src)
    if md.num_rows > f["rows"] or len(schema) > f["columns"]:
        raise _Over(f"{_shape(md.num_rows, len(schema))}: over {_n(f['rows'])} rows or {f['columns']} columns")
    if sum(md.row_group(i).total_byte_size for i in range(md.num_row_groups)) > f["declared_bytes"]:
        raise _Over(f"over {_size(f['declared_bytes'])} unpacked, as its footer says")
    if bad := [n for n, t in zip(schema.names, schema.types, strict=True) if not _plain(t, pa)]:
        raise _Over(f"list, struct or binary columns: {', '.join(bad[:5])}")
    return "ok", _shape(md.num_rows, len(schema))


def _arrow(src: Path, f: dict) -> tuple[str, str]:
    size = src.stat().st_size
    with src.open("rb") as fh:
        head = fh.read(6)
        fh.seek(max(0, size - 6))
        tail = fh.read(6)
    if size < 12 or head != b"ARROW1" or tail != b"ARROW1":
        raise _Over("not an Arrow IPC file (the streaming format and Feather v1 aren't shown)")
    pa = _pyarrow()
    if pa is None:
        return "partial", "format ok; install pyarrow to check rows, columns and types here"
    reader = pa.ipc.open_file(src)
    schema = reader.schema
    rows = sum(reader.get_batch(i).num_rows for i in range(reader.num_record_batches))
    if rows > f["rows"] or len(schema) > f["columns"]:
        raise _Over(f"{_shape(rows, len(schema))}: over {_n(f['rows'])} rows or {f['columns']} columns")
    if bad := [n for n, t in zip(schema.names, schema.types, strict=True) if not _plain(t, pa)]:
        raise _Over(f"list, struct or binary columns: {', '.join(bad[:5])}")
    return "ok", _shape(rows, len(schema))


_DIMENSION = re.compile(rb'<dimension\s+ref="(?:[A-Z]+\d+:)?([A-Z]+)(\d+)"')


def _column_number(letters: bytes) -> int:
    n = 0
    for c in letters:
        n = n * 26 + c - 64
    return n


def _xlsx(src: Path, f: dict) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(src) as z:
            members = z.infolist()
            if len(members) > f["members"]:
                raise _Over(f"over {_n(f['members'])} files inside")
            unzipped = sum(m.file_size for m in members)
            if unzipped > f["unzipped_bytes"]:
                raise _Over(f"{_size(unzipped)} unpacked: over {_size(f['unzipped_bytes'])}")
            names = {m.filename for m in members}
            if "xl/workbook.xml" not in names:
                raise _Over("not an Excel workbook")
            sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
            sheet = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in names else (sheets or [None])[0]
            head = b""
            if sheet:
                with z.open(sheet) as fh:
                    head = fh.read(8192)  # the dimension element sits near the top
    except zipfile.BadZipFile as exc:
        raise _Over("not an Excel workbook") from exc
    m = _DIMENSION.search(head)
    extra = f"; only the first of {len(sheets)} sheets is shown" if len(sheets) > 1 else ""
    if not m:
        return "partial", f"workbook ok; its size is checked on the server{extra}"
    cols, rows = _column_number(m.group(1)), int(m.group(2))
    if rows - 1 > f["rows"] or cols > f["columns"]:
        over = f"over {_n(f['rows'])} rows or {f['columns']} columns"
        raise _Over(f"its first sheet spans {_shape(rows, cols)}: {over}")
    return "ok", f"first sheet up to {_shape(rows, cols)}{extra}"


# --- the folder -------------------------------------------------------------------------


def check_table(src: Path, path: str, limits: dict) -> Result | None:
    found = kind_of(path, limits)
    if found is None:
        return None
    kind, f = found
    tables = limits["tables"]
    size = src.stat().st_size
    if size > tables["source_bytes"]:
        return Result(path, "table", f"{_size(size)}: over {_size(tables['source_bytes'])}, so a download, not a table")
    try:
        if kind == "csv":
            return Result(path, "ok", _csv(src, path, f, tables["cell_bytes"]))
        if kind == "ndjson":
            return Result(path, "ok", _ndjson(src, f, tables["cell_bytes"]))
        status, message = {"parquet": _parquet, "arrow": _arrow, "xlsx": _xlsx}[kind](src, f)
        return Result(path, status, message)
    except _Over as exc:
        return Result(path, "table", f"{exc}, so a download, not a table")
    except Exception as exc:  # a file the libraries here can't read: the server can't either
        return Result(path, "table", f"can't be read ({type(exc).__name__}), so a download, not a table")


def check(build_dir: Path, paths: list[str], limits: dict) -> list[Result]:
    """Problems with the upload as a whole, then one result per table or oversized Markdown file."""
    up = limits["upload"]
    out: list[Result] = []
    if len(paths) > up["files"]:
        out.append(Result(".", "upload", f"{_n(len(paths))} files: over {_n(up['files'])}, the upload is refused"))
    total = 0
    md = limits["markdown"]
    for path in paths:
        src = build_dir / path
        size = src.stat().st_size
        total += size
        if size > up["file_bytes"]:
            out.append(
                Result(path, "upload", f"{_size(size)}: over {_size(up['file_bytes'])} a file, the upload is refused")
            )
            continue
        if len(path) > up["path_length"] or path.count("/") + 1 > up["path_depth"]:
            out.append(Result(path, "upload", "path too long or too deep, the upload is refused"))
            continue
        if any(path.lower().endswith(e) for e in md["extensions"]) and size > md["source_bytes"]:
            out.append(Result(path, "table", f"{_size(size)}: over {_size(md['source_bytes'])}, shown as a plain file"))
            continue
        if r := check_table(src, path, limits):
            out.append(r)
    if total > up["total_bytes"]:
        out.append(
            Result(".", "upload", f"{_size(total)} in all: over {_size(up['total_bytes'])}, the upload is refused")
        )
    return out


LABELS = {"ok": "ok", "partial": "partly checked", "table": "not a table", "upload": "refused"}


def report(results: list[Result], source: str, out) -> int:
    """Print the results; 1 if any file won't be a table or the upload would be refused."""
    print(f"Checked against {source}.", file=out)
    if not results:
        print("  No table files.", file=out)
    width = max((len(LABELS[r.status]) for r in results), default=0)
    for r in results:
        print(f"  {LABELS[r.status]:<{width}}  {r.path}: {r.message}", file=out)
    return 1 if any(r.status in ("table", "upload") for r in results) else 0


def problems(results: list[Result]) -> list[Result]:
    return [r for r in results if r.status in ("table", "upload")]
