# Margent CLI

`margent push` uploads a folder to Margent: a built static site, or the sources
themselves. The server renders Markdown and table files (CSV, Parquet, Arrow, NDJSON,
Excel) into pages, so a repo of Markdown needs no build step. Run it from inside the content repo's git checkout.

```sh
uv run --project /path/to/margent/cli margent push dist   # a built site
uv run --project /path/to/margent/cli margent push .      # Markdown/CSV sources
# or: uvx --from /path/to/margent/cli margent push .
```

Git-ignored files are left out, so pushing `.` doesn't upload `.venv/` or data your
`.gitignore` keeps private. A folder that is itself ignored (a typical `dist/`) is
uploaded whole. Git-ignored `.html` files that are left out get a warning: built pages in
a tracked staging folder usually mean the folder itself should be ignored.

What it needs:

- `.margent.yml` at the git root, listing the target projects
  (`projects: [prj_…]`, optional `secrets_allowlist`).
- An `origin` remote on GitHub (`git@github.com:owner/repo.git` or
  `https://github.com/owner/repo`): it is sent as the repo, and each project only
  accepts pushes from its linked repo.
- `MARGENT_TOKEN`: a personal API token with the `builds:push` scope (app:
  **API tokens**), allowed for the projects. The project must have *direct push* on, and
  you must be its owner or editor.
- `MARGENT_SERVER` (or `--server`): the app URL, e.g. `https://app.example.com`
  or `http://127.0.0.1:8000` in dev. https is required except for 127.0.0.1, localhost
  and ::1. For token pushes the server is never taken from `.margent.yml` alone (a
  cloned repo must not decide where your token goes); if the yml names a different
  server, the push is refused.

The branch is the checked-out one (`--branch` overrides). In a clean checkout the commit
SHA is recorded; with uncommitted changes the build is marked "dirty".

After uploading, the CLI waits (up to 5 minutes) for the server's secret scan and
processing, then prints the projects that accepted the build, or the findings
(`file:line:rule (fingerprint)`; add the path glob or fingerprint to `secrets_allowlist`
for false positives). `--no-wait` returns right after the upload. Exit codes: `0`
accepted (possibly by no project; it says why to check), `1` rejected or still
processing, `2` usage, git or network errors.

Other options: `--dry-run` lists the files without uploading. `--oidc` authenticates with
the GitHub Actions OIDC token instead of `MARGENT_TOKEN` (used by `../action`; the
server may then come from the yml, since the token's audience binds it).

## margent check

`margent check [DIR]` tells you before a push which table files (CSV/TSV, Parquet, Arrow,
NDJSON, Excel) will be shown as tables and which will only be downloads, and whether the
upload would be refused (a file over the size limit, too many files). `push` runs the same
check and prints a warning for each problem; the push itself goes ahead.

The limits come from the server (`GET /api/limits`, no token sent, so the server may come
from `.margent.yml` here), falling back to built-in ones when it can't be reached. CSV and
NDJSON files are read the way the server reads them. Parquet and Arrow files get their
footer checked; with `pyarrow` installed also rows, columns and types. Excel workbooks get
their zip directory and first sheet's size checked. A binary file's unpacked size is only
known to the server. Exit code `1` when any file won't be a table or the upload would be
refused, so it can gate CI.

Tests: `uv run pytest`.
