"""What an upload and its table files may be: the server's GET /api/limits, or these defaults.

DEFAULTS is the server's answer with its default settings (server/builds/views.py `limits`;
server/builds/tests/test_cli_contract.py keeps the two equal). `fetch` asks the server, so
raising a limit there reaches every CLI without an update.
"""

import json
import urllib.error
import urllib.request

MB = 2**20

DEFAULTS = {
    "version": 1,
    "upload": {
        "upload_bytes": 100 * MB,
        "total_bytes": 500 * MB,
        "file_bytes": 50 * MB,
        "files": 10_000,
        "path_length": 300,
        "path_depth": 20,
    },
    "markdown": {"extensions": [".md", ".markdown"], "source_bytes": 5 * MB},
    "tables": {
        "source_bytes": 200 * MB,
        "cell_bytes": 16 * MB,
        "formats": {
            "csv": {
                "extensions": [".csv", ".tsv"],
                "rows": 5_000_000,
                "columns": 500,
                "text_bytes": 200 * MB,
                "line_delimiters": 10_000,
            },
            "parquet": {
                "extensions": [".parquet"],
                "rows": 5_000_000,
                "columns": 500,
                "text_bytes": 200 * MB,
                "footer_bytes": MB,
                "declared_bytes": 1024 * MB,
            },
            "arrow": {"extensions": [".arrow", ".feather"], "rows": 1_000_000, "columns": 500, "text_bytes": 100 * MB},
            "ndjson": {"extensions": [".ndjson", ".jsonl"], "rows": 1_000_000, "columns": 500, "text_bytes": 100 * MB},
            "xlsx": {
                "extensions": [".xlsx"],
                "rows": 1_000_000,
                "columns": 500,
                "text_bytes": 100 * MB,
                "unzipped_bytes": 100 * MB,
                "members": 10_000,
            },
        },
    },
}


def _valid(body) -> bool:
    try:
        t = body["tables"]
        return (
            body["version"] == 1
            and all(isinstance(body["upload"][k], int) for k in DEFAULTS["upload"])
            and isinstance(t["source_bytes"], int)
            and isinstance(t["cell_bytes"], int)
            and all(isinstance(f["extensions"], list) and isinstance(f["rows"], int) for f in t["formats"].values())
            and set(DEFAULTS["tables"]["formats"]) <= set(t["formats"])
        )
    except (KeyError, TypeError):
        return False


def fetch(server: str | None) -> tuple[dict, str]:
    """(limits, where they came from). No credentials are sent: the limits are public."""
    if not server:
        return DEFAULTS, "built-in limits (no server configured)"
    try:
        req = urllib.request.Request(f"{server}/api/limits", headers={"Accept": "application/json"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 -- scheme checked by cli.check_server
            body = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return DEFAULTS, f"built-in limits ({server} not reachable: {exc})"
    if not _valid(body):
        return DEFAULTS, f"built-in limits ({server} sent no usable limits)"
    return body, f"{server}'s limits"
