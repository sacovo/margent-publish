import gzip
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from margent import cli, gitinfo
from margent.archive import ArchiveError, build_archive, collect


def members(body: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(body))) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


@pytest.fixture
def build(tmp_path):
    d = tmp_path / "dist"
    (d / "css").mkdir(parents=True)
    (d / "index.html").write_text("<p>hi</p>")
    (d / "css/a.css").write_text("p{}")
    (d / ".DS_Store").write_text("junk")
    return d


def test_archive_contents_and_generated_manifest(build):
    body, paths = build_archive(build, "projects: []\n")
    assert paths == ["css/a.css", "index.html"]
    m = members(body)
    assert m[".margent/config.yml"] == b"projects: []\n"
    assert json.loads(m[".margent/manifest.json"]) == {"documents": [{"path": "index.html", "kind": "html"}]}
    assert m["index.html"] == b"<p>hi</p>" and ".DS_Store" not in m
    assert build_archive(build, "projects: []\n")[0] == body  # deterministic


def test_existing_manifest_is_used(build):
    (build / "manifest.json").write_text('{"documents": [{"path": "index.html", "kind": "md"}]}')
    m = members(build_archive(build, "")[0])
    assert json.loads(m[".margent/manifest.json"])["documents"][0]["kind"] == "md"


@pytest.mark.parametrize("name", ["_b", "_ac", ".margent", "_AC"])
def test_reserved_names_rejected(build, name):
    (build / name).mkdir()
    (build / name / "x").write_text("x")
    with pytest.raises(ArchiveError, match="Reserved"):
        collect(build)


def test_symlinks_rejected(build):
    os.symlink("/etc/passwd", build / "passwd")
    with pytest.raises(ArchiveError, match="Symlinks"):
        collect(build)


def test_symlinked_dir_rejected(build, tmp_path):
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", build / "linked")
    with pytest.raises(ArchiveError, match="Symlinks"):
        collect(build)


@pytest.mark.parametrize(
    "url, repo",
    [
        ("git@github.com:acme/site.git", "acme/site"),
        ("https://github.com/acme/site", "acme/site"),
        ("https://github.com/acme/my.site.git", "acme/my.site"),
        ("ssh://git@github.com/acme/site.git/", "acme/site"),
        ("https://gitlab.com/acme/site.git", ""),
    ],
)
def test_parse_remote(url, repo):
    assert gitinfo.parse_remote(url) == repo


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, build):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "remote", "add", "origin", "git@github.com:acme/site.git")
    (tmp_path / ".margent.yml").write_text("server: https://app.test\nprojects:\n  - prj_abcdefgh\n")
    (tmp_path / ".gitignore").write_text("dist/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_detect(repo):
    info = gitinfo.detect(repo / "dist")
    assert (info.root, info.repo, info.branch, info.dirty) == (repo, "acme/site", "main", False)
    assert len(info.sha) == 40
    (repo / "new.txt").write_text("x")
    assert gitinfo.detect(repo).dirty


@pytest.fixture
def uploads(monkeypatch):
    calls = []

    def fake(server, token, body, headers):
        calls.append({"server": server, "token": token, "body": body, "headers": headers})
        return 202, {"build": "bld_x", "accepted": 1, "projects": ["prj_abcdefgh"]}

    monkeypatch.setattr(cli, "upload", fake)
    return calls


def test_push(repo, uploads, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test/")
    assert cli.main(["push", "dist"]) == 0
    (call,) = uploads
    assert call["server"] == "https://app.test" and call["token"] == "acp_test"
    headers = call["headers"]
    assert headers["X-Margent-Repo"] == "acme/site" and headers["X-Margent-Branch"] == "main"
    assert len(headers["X-Margent-Sha"]) == 40 and "X-Margent-Dirty" not in headers
    assert b"prj_abcdefgh" in members(call["body"])[".margent/config.yml"]
    assert "accepted by 1 project(s)" in capsys.readouterr().out


def test_push_dirty_and_branch_override(repo, uploads, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    (repo / "README").write_text("uncommitted")
    (repo / ".margent.yml").write_text("projects:\n  - prj_abcdefgh\n")  # no `server:` to disagree with
    cli.main(["push", "dist", "--branch", "draft", "--server", "http://127.0.0.1:8000/"])
    headers = uploads[0]["headers"]
    assert headers["X-Margent-Dirty"] == "1" and "X-Margent-Sha" not in headers
    assert headers["X-Margent-Branch"] == "draft" and uploads[0]["server"] == "http://127.0.0.1:8000"


def test_dry_run_needs_no_token(repo, uploads, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("MARGENT_TOKEN", raising=False)
    assert cli.main(["push", "dist", "--dry-run"]) == 0
    assert not uploads and "index.html" in capsys.readouterr().out


def test_missing_token_and_config(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("MARGENT_TOKEN", raising=False)
    assert cli.main(["push", "dist"]) == 2
    (repo / ".margent.yml").rename(repo / ".agent-collab.yml")  # the pre-rename name is not read
    assert cli.main(["push", "dist"]) == 2
    assert "No .margent.yml" in capsys.readouterr().err


def test_old_env_vars_are_not_read(repo, uploads, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("MARGENT_TOKEN", raising=False)
    monkeypatch.delenv("MARGENT_SERVER", raising=False)
    monkeypatch.setenv("AGENT_COLLAB_TOKEN", "acp_test")
    monkeypatch.setenv("AGENT_COLLAB_SERVER", "https://app.test")
    assert cli.main(["push", "dist"]) == 2
    assert "MARGENT_SERVER" in capsys.readouterr().err and not uploads


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example",
        "http://localhost.attacker.example",
        "http://localhost@evil.com",
        "http://127.0.0.1.evil.com:8000",
        "https://user:pw@app.test",
        "https://user@app.test",
        "ftp://app.test",
        "https:///nohost",
        "https://app.test/?q=1",
    ],
)
def test_insecure_server_refused(url):
    with pytest.raises(cli.CLIError):
        cli.check_server(url)
    with pytest.raises(cli.CLIError):
        cli.upload(url, "t", b"", {})


@pytest.mark.parametrize(
    "url, normalized",
    [
        ("https://app.test/", "https://app.test"),
        ("https://app.test:8443/base", "https://app.test:8443/base"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_server_allowed(url, normalized):
    assert cli.check_server(url) == normalized


def test_token_push_ignores_yml_server(repo, uploads, monkeypatch, capsys):
    """The repo's yml must not choose where the personal token goes."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.delenv("MARGENT_SERVER", raising=False)
    assert cli.main(["push", "dist"]) == 2
    assert "MARGENT_SERVER" in capsys.readouterr().err and not uploads


def test_token_push_refuses_mismatching_yml_server(repo, uploads, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://other.test")
    assert cli.main(["push", "dist"]) == 2
    err = capsys.readouterr().err
    assert "https://app.test" in err and "https://other.test" in err and not uploads


def test_yml_server_alias_bomb_is_refused(repo, uploads, monkeypatch, capsys):
    """str() of nested YAML aliases would expand a few bytes into gigabytes."""
    bomb = "".join(f"a{i}: &a{i} [{','.join([f'*a{i - 1}'] * 9) if i else 'x'}]\n" for i in range(10))
    (repo / ".margent.yml").write_text(bomb + "server: *a9\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test")
    assert cli.main(["push", "dist"]) == 2
    assert "must be a URL" in capsys.readouterr().err and not uploads


def test_oidc_push_may_use_yml_server(repo, uploads, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("MARGENT_SERVER", raising=False)
    monkeypatch.setattr(cli, "oidc_token", lambda server: f"jwt-for-{server}")
    assert cli.main(["push", "dist", "--oidc"]) == 0
    assert uploads[0]["server"] == "https://app.test" and uploads[0]["token"] == "jwt-for-https://app.test"


def test_oidc_token(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.headers["Authorization"]
        return Resp(b'{"value": "jwt-here"}')

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    env = {
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://runner/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "rt",
    }
    assert cli.oidc_token("https://app.test", env) == "jwt-here"
    assert seen["url"] == "https://runner/token?api-version=2.0&audience=https%3A%2F%2Fapp.test%2Fingest"
    assert seen["auth"] == "Bearer rt"
    with pytest.raises(cli.CLIError, match="id-token"):
        cli.oidc_token("https://app.test", {})


def test_report_findings(capsys):
    data = {"build": "bld_x", "error": "Secrets found.", "findings": ["js/a.js:2:aws-access-key-id (aws:1234)"]}
    assert cli.report(422, data) == 1
    out = capsys.readouterr().out
    assert "js/a.js:2:aws-access-key-id" in out and "secrets_allowlist" in out


def test_report_none_accepted(capsys):
    assert cli.report(200, {"build": "bld_x", "accepted": 0}) == 0
    assert "No project accepted" in capsys.readouterr().out


def test_paths_are_posix(build):
    assert all("\\" not in p for p in collect(Path(build)))


@pytest.fixture
def server(monkeypatch):
    """Fake server: the upload answers `processing`, then each poll pops the next status."""
    calls, polls = [], []

    def fake(url, token, body=None, headers=None):
        calls.append((url, token))
        if url.endswith("/api/ingest"):
            return 202, {"build": "bld_x", "accepted": 1, "status": "processing"}
        return 200, polls.pop(0)

    monkeypatch.setattr(cli, "_call", fake)
    monkeypatch.setattr(cli, "POLL_INTERVAL", 0)
    return calls, polls


def test_push_waits_for_scan_and_reports_findings(repo, server, monkeypatch, capsys):
    calls, polls = server
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test")
    finding = "js/a.js:2:aws-access-key-id (aws:1234)"
    polls += [
        {"build": "bld_x", "status": "processing"},
        {"build": "bld_x", "status": "rejected", "error": "Secrets found.", "findings": [finding]},
    ]
    assert cli.main(["push", "dist"]) == 1
    assert [u for u, _ in calls] == ["https://app.test/api/ingest"] + ["https://app.test/api/builds/bld_x"] * 2
    out = capsys.readouterr().out
    assert "Secrets found." in out and finding in out


def test_oidc_poll_uses_fresh_tokens(repo, server, monkeypatch, capsys):
    calls, polls = server
    monkeypatch.chdir(repo)
    tokens = iter(range(10))
    monkeypatch.setattr(cli, "oidc_token", lambda s: f"jwt{next(tokens)}")
    polls.append({"build": "bld_x", "status": "ready", "accepted": 1})
    assert cli.main(["push", "dist", "--oidc"]) == 0
    assert [t for _, t in calls] == ["jwt0", "jwt1"]  # OIDC tokens are single-use
    assert "accepted by 1 project(s)" in capsys.readouterr().out


def test_no_wait(repo, server, monkeypatch, capsys):
    calls, _ = server
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test")
    assert cli.main(["push", "dist", "--no-wait"]) == 1  # outcome unknown
    assert len(calls) == 1 and "still processing" in capsys.readouterr().out


def test_source_folder_push_skips_git_ignored_files(repo, uploads, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test")
    (repo / ".gitignore").write_text("dist/\n.venv/\ndata/private/\n*.secret\n")
    for path in ("docs/a.md", "data/public.csv", "data/private/members.csv", ".venv/lib/x.py", "docs/b.secret"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("x")
    assert cli.main(["push", "."]) == 0
    files = {n for n in members(uploads[0]["body"]) if not n.startswith(".margent/")}
    assert files == {".gitignore", ".margent.yml", "docs/a.md", "data/public.csv"}


def test_ignored_build_dir_is_uploaded_whole(repo, uploads, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test")
    assert gitinfo.ignored(repo / "dist") == set()  # dist/ itself is ignored: nothing is filtered
    assert cli.main(["push", "dist"]) == 0
    assert {"index.html", "css/a.css"} <= set(members(uploads[0]["body"]))


def test_token_is_never_sent_on_a_redirect():
    """urllib would copy the Authorization header to wherever a redirect points."""
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/api/builds/b1":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        status, _ = cli._call(f"http://127.0.0.1:{server.server_port}/api/builds/b1", "acp_secret")
    finally:
        server.server_close()
    assert status == 302 and seen == [("/api/builds/b1", "Bearer acp_secret")]


def test_action_requirements_match_the_lock():
    """The publish action installs exactly the locked, hash-pinned dependencies."""
    import re
    import tomllib

    root = Path(__file__).resolve().parents[2]
    lock = tomllib.loads((root / "cli/uv.lock").read_text())
    deps = {p["name"]: p for p in lock["package"] if p["name"] != "margent" and "source" in p}
    runtime = {d["name"] for d in next(p for p in lock["package"] if p["name"] == "margent")["dependencies"]}
    reqs = (root / "action/requirements.txt").read_text()
    pinned = dict(re.findall(r"^([a-z0-9_-]+)==(\S+)", reqs, re.M))
    assert pinned == {name: deps[name]["version"] for name in runtime}
    for name in runtime:
        for wheel in deps[name]["wheels"]:
            assert wheel["hash"].replace("sha256:", "--hash=sha256:") in reqs


def test_git_ignored_html_is_reported(repo, uploads, monkeypatch, capsys):
    """Built decks under a tracked staging folder vanish silently otherwise (index.html ignored)."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test")
    # slides/.gitignore keeps Marp output out of the repo, and staging copies it along.
    for path, text in (("_stage/slides/.gitignore", "*/index.html\n"), ("_stage/slides/intro/index.html", "x")):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    assert cli.main(["push", "_stage"]) == 0
    err = capsys.readouterr().err
    assert "slides/intro/index.html" in err and "`_stage/` in .gitignore" in err
    assert "slides/intro/index.html" not in members(uploads[0]["body"])


def test_no_warning_for_an_ignored_build_dir(repo, uploads, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MARGENT_TOKEN", "acp_test")
    monkeypatch.setenv("MARGENT_SERVER", "https://app.test")
    assert cli.main(["push", "dist"]) == 0
    assert "Warning" not in capsys.readouterr().err


def test_ignored_html_lists_each_file_once(tmp_path):
    """git can report a folder and a file inside it; the warning counts the file once."""
    from margent.archive import ignored_html

    (tmp_path / "deck").mkdir()
    (tmp_path / "deck" / "index.html").write_text("x")
    assert ignored_html(tmp_path, {"deck/", "deck/index.html"}) == ["deck/index.html"]
