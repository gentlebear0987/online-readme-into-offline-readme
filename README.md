# md_downloader

Scans a markdown file for `[[paper]]` and `[[project]]` tags, downloads
whatever URL follows each one, and writes a local copy of the markdown
with every link pointing at the downloaded file instead of the internet.

## Expected format

```markdown
[[paper]] https://arxiv.org/abs/1706.03762
[[project]] https://github.com/octocat/Hello-World
```

The URL can be on the same line or the next line — whichever comes first
after the tag is what gets downloaded. Tags are case-insensitive and also
accept the plural form (`[[papers]]`, `[[projects]]`).

| Tag           | What happens                                                                 | Saved to              |
|---------------|-------------------------------------------------------------------------------|------------------------|
| `[[paper]]`   | Downloaded as a `.pdf`. arXiv `/abs/...` links are auto-resolved to `/pdf/...`, OpenReview `forum?id=...` links to `pdf?id=...`. If the result doesn't actually look like a PDF, it's saved under the correct extension anyway and a warning is printed — never silently mislabeled. | `downloads/papers/`   |
| `[[project]]` | The GitHub repo, downloaded as a `.zip` (via codeload.github.com)             | `downloads/projects/` |

Any other plain markdown links (`[text](url)`, `<url>`) elsewhere in the file
are also picked up and downloaded — classified as a github repo / known file
type / webpage — so nothing in the file gets silently skipped.

## Quick start

```bash
chmod +x run.sh        # only needed once
./run.sh notes.md ./notes_offline
```

`run.sh` creates an isolated `.venv/` on first run and installs dependencies
into it automatically — no manual `pip install` needed, and your system
Python is untouched. Later runs reuse the same venv and skip straight to
execution.

## What you get

```
notes_offline/
├── notes.md              <- local copy, [[paper]]/[[project]] URLs replaced
├── manifest.json          <- status of every link (ok / error, with reason)
└── downloads/
    ├── papers/             <- [[paper]] links, as .pdf
    ├── projects/           <- [[project]] links, as .zip
    ├── repos/              <- other bare github.com links, as .zip
    ├── files/               <- other links with a known file extension
    └── html/                <- other links (saved webpages)
```

Open `notes_offline/notes.md` — every `[[paper]]`/`[[project]]` line now
points at the file on disk, so it opens locally with no internet needed.
Links that failed to download are left pointing at their original URL.

## Options

```bash
./run.sh notes.md ./notes_offline \
  --workers 8 \                   # concurrent downloads
  --branch main \                 # force a branch for ALL [[project]]/repo links
  --github-token $GITHUB_TOKEN \  # raise GitHub's rate limit (60/hr -> 5000/hr)
  --timeout 30 \
  --dry-run \                     # just show classification, download nothing (output_dir not required)
  -v                               # verbose logging
```

`GITHUB_TOKEN` can also be set as an environment variable. Without one,
GitHub's unauthenticated API limit is 60 requests/hour — fine for a few
repos, but get a token (no scopes needed, just a classic PAT) if your file
has many `[[project]]` links whose default branch needs to be looked up.

## Manual install (without run.sh)

```bash
pip install -r requirements.txt
python md_downloader.py notes.md ./notes_offline
```

## Known limitations (by design, kept simple)

- `[[project]]` only supports GitHub repos.
- PDF resolution has explicit rules for arXiv and OpenReview; other paper
  hosts are downloaded as-is (most academic PDF hosts serve the file
  directly with no landing page, so this covers the common case).
- HTML pages are saved as the raw server response (no JS execution) — fine
  for most docs/articles, not for JS-rendered SPAs.
- Reference-style markdown links (`[text][ref]` + `[ref]: url`) aren't parsed
  for the *generic* fallback path (the `[[paper]]`/`[[project]]` path doesn't
  care about link style at all, since it just looks for the next URL).
