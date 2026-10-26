"""margent push [DIR] [--server URL] [--branch B] [--dry-run] [--oidc]
margent check [DIR] [--server URL]

Token: MARGENT_TOKEN (a personal API token with the builds:push scope),
or --oidc inside GitHub Actions (needs `permissions: id-token: write`).

Server: --server or MARGENT_SERVER. A token push never takes the server from
the repo's .margent.yml alone (a cloned repo must not decide where your token
goes), and refuses if the yml names a different one. OIDC pushes may use it: the
token's audience is bound to that server.

The server scans and publishes in the background; push polls GET /api/builds/<id>
(same credentials; a fresh OIDC token per poll) and prints the outcome, unless --no-wait.

check compares the folder with the server's limits (GET /api/limits, public: no token
is sent, so the server may come from the yml too): which files will be tables, and
whether the upload would be refused. push runs the same check and warns.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from . import check as checker
from . import gitinfo, limits
from .archive import ArchiveError, build_archive, collect, ignored_html

CONFIG_NAME = ".margent.yml"
TIMEOUT = 300
POLL_INTERVAL = 2
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class CLIError(Exception):
    pass


def check_server(url: str) -> str:
    """Return the normalized server URL; https, or http only to a loopback host. No credentials."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host, _ = parts.hostname, parts.port  # .port raises on a malformed port
    except ValueError as exc:
        raise CLIError(f"Invalid server URL: {url!r}") from exc
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CLIError("The server URL must not contain credentials.")
    if parts.query or parts.fragment or not host:
        raise CLIError(f"Invalid server URL: {url!r}")
    if not (parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK)):
        raise CLIError("The server URL must use https (http only for 127.0.0.1, localhost or ::1).")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def resolve_server(args, config: dict, env=os.environ) -> str:
    configured = args.server or env.get("MARGENT_SERVER")
    repo_server = config.get("server") or ""
    if not isinstance(repo_server, str):  # never str() it: YAML aliases can expand to gigabytes
        raise CLIError(f"`server` in {CONFIG_NAME} must be a URL.")
    if args.oidc or args.dry_run:
        url = configured or repo_server
        if not url:
            raise CLIError(f"No server: pass --server, set MARGENT_SERVER or `server:` in {CONFIG_NAME}.")
        return check_server(url)
    if not configured:
        raise CLIError(
            "No server: pass --server or set MARGENT_SERVER. For token pushes the server is not"
            f" taken from {CONFIG_NAME} (the repo must not choose where your token is sent)."
        )
    server = check_server(configured)
    if repo_server and check_server(repo_server) != server:
        raise CLIError(
            f"{CONFIG_NAME} names server {check_server(repo_server)} but you configured {server}."
            " Refusing to push; fix the yml or your --server / MARGENT_SERVER."
        )
    return server


def oidc_token(server: str, env=os.environ) -> str:
    """Request a GitHub Actions OIDC token whose audience is `<server>/ingest`."""
    url, bearer = env.get("ACTIONS_ID_TOKEN_REQUEST_URL"), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise CLIError("No OIDC environment. Run in GitHub Actions with `permissions: id-token: write`.")
    sep = "&" if "?" in url else "?"
    audience = urllib.parse.quote(f"{server}/ingest", safe="")
    req = urllib.request.Request(  # noqa: S310
        f"{url}{sep}audience={audience}", headers={"Authorization": f"Bearer {bearer}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 -- URL from the Actions runner
        return json.load(resp)["value"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies Authorization onto any redirect target; a redirect is an error here."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _call(url: str, token: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(  # noqa: S310 -- scheme checked by check_server
        url, data=body, method="POST" if body is not None else "GET", headers={"Authorization": f"Bearer {token}"}
    )
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:  # scheme checked by check_server
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except ValueError:
            return exc.code, {"error": exc.reason}


def upload(server: str, token: str, body: bytes, headers: dict) -> tuple[int, dict]:
    return _call(f"{check_server(server)}/api/ingest", token, body, {"Content-Type": "application/gzip", **headers})


def wait(server: str, get_token, build: str, timeout: float = TIMEOUT, interval: float = POLL_INTERVAL):
    """Poll GET /api/builds/<id> until the server-side scan and processing are done."""
    deadline = time.monotonic() + timeout
    while True:
        status, data = _call(f"{check_server(server)}/api/builds/{urllib.parse.quote(build)}", get_token())
        if status != 200 or data.get("status") != "processing" or time.monotonic() >= deadline:
            return status, data
        time.sleep(interval)


def report(status: int, data: dict, out=None) -> int:
    out = out or sys.stdout
    if data.get("status") == "processing":
        print(
            f"Build {data.get('build')} is still processing (secret scan, publishing); check the project page.",
            file=out,
        )
        return 1
    if status >= 400 or data.get("status") == "rejected":
        print(f"Upload rejected{f' ({status})' if status >= 400 else ''}: {data.get('error', 'error')}", file=out)
        for finding in data.get("findings", []):
            print(f"  {finding}", file=out)
        if data.get("findings"):
            print("False positive? Add the path or the fingerprint to secrets_allowlist in .margent.yml.", file=out)
        return 1
    accepted = data.get("accepted", 0)
    # No build ID when the server refused before creating one (no project could accept it).
    what = f"Build {data['build']}" if data.get("build") else "Upload"
    print(f"{what}: accepted by {accepted} project(s).", file=out)
    for project in data.get("projects", []):
        print(f"  {project}", file=out)
    if not accepted:
        print(
            "No project accepted it. Check: listed in .margent.yml, repo linked, branch configured"
            " (and for direct pushes: allow_direct_push, owner/editor role, token project allowlist).",
            file=out,
        )
    return 0


def push(args) -> int:
    info = gitinfo.detect(Path.cwd())
    config_path = info.root / CONFIG_NAME
    if not config_path.is_file():
        raise CLIError(f"No {CONFIG_NAME} at the git root ({info.root}).")
    config_text = config_path.read_text()
    config = yaml.safe_load(config_text) or {}
    if not isinstance(config, dict):
        raise CLIError(f"{CONFIG_NAME} must be a mapping (key: value).")
    server = resolve_server(args, config)
    build_dir = Path(args.dir)
    if not build_dir.is_dir():
        raise CLIError(f"{build_dir} is not a directory.")
    branch = args.branch or info.branch
    # A source folder (PLAN §2.8) uploads without what .gitignore keeps out of the repo.
    ignored = gitinfo.ignored(build_dir)
    body, paths = build_archive(build_dir, config_text, ignored)
    if skipped := ignored_html(build_dir, ignored):
        # Built pages under a tracked folder are usually git-ignored output, and dropping them
        # silently looks like a rendering problem on the server.
        shown = ", ".join(skipped[:5]) + (f" and {len(skipped) - 5} more" if len(skipped) > 5 else "")
        folder = build_dir.as_posix().rstrip("/")
        example = f" (`{folder}/` in .gitignore)" if folder not in ("", ".") else ""
        print(
            f"Warning: {len(skipped)} git-ignored HTML file(s) left out: {shown}. "
            f"To upload a build folder whole, git-ignore the folder itself{example}.",
            file=sys.stderr,
        )
    headers = {"X-Margent-Branch": branch, "X-Margent-Repo": info.repo}
    if info.sha and not info.dirty:
        headers["X-Margent-Sha"] = info.sha
    else:
        headers["X-Margent-Dirty"] = "1"
    state = "dirty" if info.dirty else info.sha[:12]
    print(f"{len(paths)} files, {len(body)} bytes; {info.repo or '?'} {branch or '?'} ({state}) -> {server}")
    limit_set, source = limits.fetch(server)
    for r in checker.problems(checker.check(build_dir, paths, limit_set)):
        print(f"Warning: {r.path}: {r.message} (margent check {args.dir} for details).", file=sys.stderr)
    if args.dry_run:
        for p in paths:
            print(f"  {p}")
        return 0
    if args.oidc:
        token = oidc_token(server)
    else:
        token = os.environ.get("MARGENT_TOKEN", "")
        if not token:
            raise CLIError("Set MARGENT_TOKEN to an API token with the builds:push scope.")
        if not branch:
            raise CLIError("Could not detect the branch; pass --branch.")
    status, data = upload(server, token, body, headers)
    if status == 202 and data.get("status") == "processing" and not args.no_wait:
        print(f"Uploaded build {data.get('build')}; waiting for the server's secret scan...")
        status, data = wait(server, (lambda: oidc_token(server)) if args.oidc else (lambda: token), data["build"])
    return report(status, data)


def check(args) -> int:
    build_dir = Path(args.dir)
    if not build_dir.is_dir():
        raise CLIError(f"{build_dir} is not a directory.")
    server, ignored = args.server or os.environ.get("MARGENT_SERVER"), set()
    try:
        info = gitinfo.detect(Path.cwd())
        ignored = gitinfo.ignored(build_dir)
        config_path = info.root / CONFIG_NAME
        config = yaml.safe_load(config_path.read_text()) if config_path.is_file() else {}
        repo_server = config.get("server") if isinstance(config, dict) else None
        server = server or (repo_server if isinstance(repo_server, str) else None)
    except (subprocess.CalledProcessError, OSError, yaml.YAMLError):  # not a git checkout: the folder as it is
        ignored = set()
    paths = collect(build_dir, ignored)
    limit_set, source = limits.fetch(check_server(server) if server else None)
    return checker.report(checker.check(build_dir, paths, limit_set), source, sys.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="margent")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("push", help="Upload a built directory.")
    p.add_argument("dir", nargs="?", default="dist", help="Build output directory (default: dist).")
    p.add_argument("--server", help="Server URL (default: MARGENT_SERVER; with --oidc also `server:` in the yml).")
    p.add_argument("--branch", help="Branch to publish as (default: current branch; ignored with --oidc).")
    p.add_argument("--dry-run", action="store_true", help="Build the archive and list files; don't upload.")
    p.add_argument("--no-wait", action="store_true", help="Don't wait for the server's scan and publish.")
    p.add_argument("--oidc", action="store_true", help="Authenticate with the GitHub Actions OIDC token.")
    c = sub.add_parser("check", help="Check which files will be tables, and that the upload fits the limits.")
    c.add_argument("dir", nargs="?", default="dist", help="Folder to check (default: dist).")
    c.add_argument("--server", help="Server whose limits to use (default: MARGENT_SERVER, then the yml's).")
    args = parser.parse_args(argv)
    try:
        return check(args) if args.command == "check" else push(args)
    except (CLIError, ArchiveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # git/network failures: short message, no traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
