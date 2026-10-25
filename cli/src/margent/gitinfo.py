"""Detect repo, branch, SHA and dirty state from git."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_REMOTE_RE = re.compile(r"github\.com[:/]+(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")


@dataclass
class GitInfo:
    root: Path
    repo: str  # owner/repo, "" if not a GitHub remote
    branch: str
    sha: str
    dirty: bool


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def parse_remote(url: str) -> str:
    """`git@github.com:acme/site.git` / `https://github.com/acme/site` -> `acme/site`."""
    m = _REMOTE_RE.search(url.strip())
    return m.group("repo") if m else ""


def detect(cwd: Path) -> GitInfo:
    root = Path(_git(cwd, "rev-parse", "--show-toplevel"))
    try:
        remote = _git(root, "remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        remote = ""
    try:
        sha = _git(root, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:  # no commits yet
        sha = ""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD") if sha else ""
    dirty = bool(_git(root, "status", "--porcelain")) or not sha
    return GitInfo(
        root=root, repo=parse_remote(remote), branch="" if branch == "HEAD" else branch, sha=sha, dirty=dirty
    )


def ignored(build_dir: Path) -> set[str]:
    """Git-ignored entries under build_dir, relative POSIX paths (directories end in "/").

    Empty when build_dir is outside a work tree or is itself ignored (a typical
    dist/): then the whole folder is the build.
    """
    try:
        _git(build_dir, "rev-parse", "--is-inside-work-tree")
    except (subprocess.CalledProcessError, NotADirectoryError):
        return set()
    if subprocess.run(["git", "check-ignore", "-q", "."], cwd=build_dir, capture_output=True).returncode == 0:
        return set()
    out = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
        cwd=build_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {p for p in out.split("\0") if p}
