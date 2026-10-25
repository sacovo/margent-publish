"""Build the upload: a tar.gz of the build dir plus the reserved metadata members.

Server contract (server/builds/archive.py): regular files and directories only,
`.margent/config.yml` = the repo's `.margent.yml`, and an optional
`.margent/manifest.json`. Top-level `_b/`, `_ac/` and `.margent/`
are reserved.
"""

import gzip
import io
import json
import os
import tarfile
from pathlib import Path

META_DIR = ".margent"
RESERVED = {"_b", "_ac", META_DIR}
SKIP_NAMES = {".git", ".DS_Store"}


class ArchiveError(Exception):
    pass


def collect(build_dir: Path, ignored: set[str] = frozenset()) -> list[str]:
    """Relative POSIX paths of all files, sorted, minus `ignored` (gitinfo.ignored).

    Rejects links and reserved names.
    """
    paths = []
    for root, dirs, files in os.walk(build_dir):
        here = Path(root).relative_to(build_dir).as_posix()
        prefix = "" if here == "." else here + "/"
        dirs[:] = sorted(d for d in dirs if d not in SKIP_NAMES and f"{prefix}{d}/" not in ignored)
        files[:] = [f for f in files if prefix + f not in ignored]
        for name in [*dirs, *files]:
            full = Path(root) / name
            rel = full.relative_to(build_dir).as_posix()
            if full.is_symlink():
                raise ArchiveError(f"Symlinks are not supported: {rel}")
            if "/" not in rel and name.casefold() in RESERVED:
                raise ArchiveError(f"Reserved top-level name in build: {rel}")
            if name in files and name not in SKIP_NAMES:
                if not full.is_file():
                    raise ArchiveError(f"Not a regular file: {rel}")
                paths.append(rel)
    return sorted(paths)


def default_manifest(paths: list[str]) -> dict:
    docs = [{"path": p, "kind": "html"} for p in paths if p.lower().endswith((".html", ".htm"))]
    return {"documents": docs}


def _info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, 0o644, 0
    return info


def build_archive(build_dir: Path, config_text: str, ignored: set[str] = frozenset()) -> tuple[bytes, list[str]]:
    """Return (tar.gz bytes, file list). Uses build_dir/manifest.json if present."""
    paths = collect(build_dir, ignored)
    manifest_file = build_dir / "manifest.json"
    if manifest_file.is_file():
        manifest = manifest_file.read_bytes()
    else:
        manifest = json.dumps(default_manifest(paths), indent=1).encode()
    buf = io.BytesIO()
    # mtime=0 and sorted members: the same build gives the same bytes.
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name, data in ((f"{META_DIR}/config.yml", config_text.encode()), (f"{META_DIR}/manifest.json", manifest)):
            tar.addfile(_info(name, len(data)), io.BytesIO(data))
        for rel in paths:
            full = build_dir / rel
            with full.open("rb") as f:
                tar.addfile(_info(rel, full.stat().st_size), f)
    return buf.getvalue(), paths
