# Setting up a repository to publish to Margent

For people and coding agents adding the publish workflow to a content repository.
Margent shows what the workflow uploads, and anchors review comments to it. Margent
copes with any upload, but it can only render, and anchor comments to, the things this guide covers.

## The two files

- `.margent.yml` at the repository root: `server:` and the `projects:` IDs to deliver
  to. Margent's setup page gives the exact text.
- A workflow (`.github/workflows/margent-publish.yml` unless the project names another
  path) with `permissions: contents: read, id-token: write` and the publish action
  pinned by full commit SHA. The `dir:` input is the folder that gets uploaded.

## What to upload

Look at what the repository contains and pick per kind of content:

| Content | What to do |
|---|---|
| Markdown (`.md`) and CSV/TSV | Nothing: upload the sources. Margent renders them (`a/b.md` → `a/b.html`, `a/b.csv` → `a/b.csv.html`). |
| Static HTML that a tool builds (site generator, script) | Build it in the workflow and upload the output folder. |
| **Marp decks** (`.md` with `marp: true` front matter) | **Build them** as described below. Margent renders a Marp source as a plain Markdown page, which looks wrong. |
| Mixed | Stage one folder: copy the sources you want reviewed, build the rest into it, upload that. |

Never upload `node_modules`, build caches, `.env` files or other local state. Staging from
`git ls-files` is a reliable way to leave those out.

**Git-ignore the upload folder itself** (for example `_margent/` in the root `.gitignore`).
The action applies `.gitignore` inside a tracked folder, so built pages that a rule like
`*/index.html` keeps out of the repository would be dropped from the upload. The CLI warns about
git-ignored HTML it leaves out. A folder that is ignored as a whole is uploaded in full. A built file beats a source file at the
same output path, so `deck.md` next to your own `deck.html` uploads the built `deck.html`.

## Marp decks

Comments on a deck anchor to the slide and to the source lines. That needs Margent's
markdown-it plugin, `marp/sourcepos.mjs` in this repository. Copy it from the same commit
you pin the action to, as `margent-sourcepos.mjs` next to a Marp config used only for the
Margent build:

```js
// margent.marp.config.mjs
import sourcepos from './margent-sourcepos.mjs';

export default {
  lang: 'en',               // default language; a deck's own `lang:` front matter wins
  allowLocalFiles: true,
  // A --config-file replaces .marprc.yml / marp.config.*, so repeat their settings here.
  // Leaving one out fails silently: without `html: true`, HTML blocks in a deck just vanish.
  // html: true,
  // themeSet: 'theme',
  engine: ({ marp }) => marp.use(sourcepos),
};
```

In the workflow, build each deck into the staged folder, and leave the deck's `.md` source out
of it. Otherwise the source is uploaded too, and Margent shows it as a second, unstyled page:

```bash
npx --no-install marp --no-stdin --config-file margent.marp.config.mjs \
  slides/intro.md -o _margent/slides/intro/index.html
```

Keep relative asset paths working: build into a folder that mirrors the repository layout,
and copy the assets the deck uses.

Then tell Margent which pages are decks with a `manifest.json` at the root of the uploaded folder:

```json
{"documents": [
  {"path": "slides/intro/index.html", "kind": "marp", "title": "Intro",
   "source_file": "slides/intro.md"}
]}
```

`source_file` is the deck's path in the repository, so review comments point back to its lines.

A complete workflow for a repository with Markdown pages and decks at `slides/<deck>/slides.md`
(npm project in `slides/` with `@marp-team/marp-cli`; adjust paths and branch):

```yaml
name: Publish to Margent
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803  # v6.1.0
      - uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1  # v7.1.0
        with:
          node-version: 22
          cache: npm
          cache-dependency-path: slides/package-lock.json
      # Tracked files only (no node_modules or local output), without the deck sources.
      - name: Stage
        run: |
          mkdir -p _margent
          git ls-files -z -- . ':!:slides/*/slides.md' | xargs -0 cp --parents -t _margent
      - name: Build decks
        working-directory: slides
        run: |
          npm ci --no-audit --no-fund
          for src in */slides.md; do
            deck=$(dirname "$src")
            npx --no-install marp --no-stdin --config-file margent.marp.config.mjs \
              "$src" -o "../_margent/slides/$deck/index.html"
            jq -n --arg deck "$deck" --arg title "$(sed -n 's/^title: *//p' "$src" | head -1 | sed -E "s/^([\"'])(.*)\1$/\2/")" \
              '{path: "slides/\($deck)/index.html", kind: "marp", title: $title,
                source_file: "slides/\($deck)/slides.md"}'
          done | jq -s '{documents: .}' > ../_margent/manifest.json
      - uses: OWNER/margent-publish/action@COMMIT_SHA  # pin by SHA
        with:
          dir: _margent
```

Add `_margent/` to the root `.gitignore` (see above). Commit the plugin copy under a name
your `.gitignore` doesn't match. For example, Python's
template ignores `lib/`.

## manifest.json

Optional. Every `.html` file is a document either way; the manifest adds to it:

| Key | |
|---|---|
| `path` | the HTML file in the upload |
| `kind` | `html`, `md`, `marp`, `table` or `viz` |
| `title` | shown in Margent's document list |
| `source_file` | the repository file it was built from |

## Checks before you finish

- Pin every action by full commit SHA, and keep the job's `permissions` as given.
- Run the build locally into the staging folder, and look at it: open a deck's `index.html` and
  check that it is slides, not a long page.
- List what would be uploaded, without uploading: `margent push _margent --dry-run` (the CLI is
  `cli/` in this repository). Each deck's `index.html` and `manifest.json` must be in the list.
- Commit on a branch. Each push to a configured branch publishes a version.
