# margent-publish action

Uploads a folder to Margent from GitHub Actions: the repo's Markdown and CSV
sources (the server renders them), or a static build. It authenticates
with a GitHub OIDC token (audience `<server>/ingest`), so the repo needs no
secrets. The server delivers the build to every project that is listed in the
repo's `.margent.yml`, linked to this repository's ID, and has the branch
configured.

## Inputs

| Input | Required | Default | |
|---|---|---|---|
| `dir` | yes | | Folder to publish: `.` for Markdown/CSV sources, or your build's output directory. |
| `server` | no | `server:` in `.margent.yml` | Server URL. |
| `gitleaks` | no | `true` | Run gitleaks (pinned version, checksum-verified) on `dir` before uploading. The server scans again regardless. |

## Example workflows

A repo of Markdown and CSV needs no build step:

```yaml
name: Publish to Margent
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions: {}
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803  # v6.1.0
        with: { persist-credentials: false }
      - uses: OWNER/margent-publish/action@COMMIT_SHA  # pin by SHA
        with:
          dir: .
```

With a build of your own, `.github/workflows/margent-publish.yml`:

```yaml
name: publish
on:
  push:
    branches: [main]        # the branches configured as channels
  workflow_dispatch:
# Never pull_request / pull_request_target / workflow_run: they can build fork
# code, and the server rejects their tokens anyway.

permissions: {}             # nothing by default

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803  # v6.1.0
        with: { persist-credentials: false }
      - run: make build     # your build; output in ./dist
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9  # v7.0.2
        with: { name: site, path: dist }

  publish:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write       # only this job can mint the OIDC token
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803  # v6.1.0
        with: { persist-credentials: false }
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333  # v8.0.2
        with: { name: site, path: dist }
      - uses: OWNER/margent-publish/action@COMMIT_SHA  # pin by SHA
        with:
          dir: dist
```

A single job works too, as long as `id-token: write` is only granted to the
job that publishes and every third-party action is pinned by commit SHA. If
the build runs untrusted dependencies, the two-job split keeps them away from
the OIDC token.

The checkout is needed in the publish job because the CLI reads
`.margent.yml` from the repo root.

## Notes

- SHA, branch, actor, run ID and workflow come from the token's claims, never
  from the upload.
- The response only says the build ID and how many projects accepted it
  (Actions logs of public repos are public). The server scans in the background;
  the step polls the build's status (a fresh OIDC token per poll) and fails if the
  build is rejected. Secret-scan rejections list
  `file:line:rule (fingerprint)`, never the secret. Add a path glob or the
  fingerprint to `secrets_allowlist` for false positives.
- Projects can additionally pin the workflow file and a protected GitHub
  Environment for main in their settings.
- Anyone with write access to the linked repo can publish to the configured
  branches.
- Runs on Linux runners with Python 3.11+ (preinstalled on GitHub-hosted Ubuntu). It installs only `pyyaml`, pinned by hash in `action/requirements.txt`, and runs the CLI from this repository.
