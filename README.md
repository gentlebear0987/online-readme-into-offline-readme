# md_downloader

Makes a markdown file fully browsable offline. Every URL in the file gets
downloaded and the link rewritten to point at the local copy — not just
`[[paper]]`/`[[project]]` tagged ones, but every plain link too.

## Quick start

```bash
chmod +x run.sh        # only needed once
./run.sh notes.md ./notes_offline
```

Skip the second argument to write a zip beside the markdown's parent folder:

```bash
./run.sh work/myproject/notes.md
# -> work/myproject_1.zip
```

Process several inputs at once (comma-separated, quote-aware):

```bash
./run.sh "'projA.zip', 'projB/README.md', projC"
```

`run.sh` creates an isolated `.venv/` on first run and installs dependencies
into it automatically. Later runs reuse it and skip straight to execution.

## Input formats

The first argument accepts one or more items separated by `,` or `, `.
Commas inside `'...'` or `"..."` are not treated as separators.

Each item can be:

| Input | Resolves to |
|-------|-------------|
| `notes.md` | That markdown file |
| `myproject/` | `README.md` or the only `.md` in the folder |
| `myproject.zip` (existing file) | Unzip → find `README.md` → process |
| `myproject.zip` (no file) | Folder `myproject/` with the same stem |

## Output

### Explicit output directory (second argument)

```
notes_offline/
├── notes.md              <- local copy, every URL replaced with a local path
├── manifest.json          <- status of every link (ok/error, content-type, etc.)
└── downloads/
    ├── papers/             <- PDFs (papers, arXiv, OpenReview)
    ├── projects/           <- GitHub repo zips
    ├── hf_models/          <- Hugging Face model repos
    ├── hf_datasets/        <- Hugging Face dataset repos
    ├── hf_spaces/          <- Hugging Face Space repos
    ├── files/              <- direct file downloads
    └── html/               <- webpages (docs, blog, etc.)
```

### Default (no second argument)

Each input is processed into a temporary folder, then saved as a zip named
`<parent-folder>_1.zip` placed beside that folder:

| Input | Output zip |
|-------|------------|
| `work/myproject/notes.md` | `work/myproject_1.zip` |
| `work/myproject.zip` | `work/myproject_1.zip` |

The original zip is never overwritten.

## What happens to each URL

| URL is... | What happens | Saved to |
|-----------|--------------|----------|
| Tagged `[[paper]]` | Forced PDF. arXiv `/abs/...` and OpenReview `forum?id=...` resolved first. | `downloads/papers/` |
| Bare arXiv `/abs/...`, `/pdf/...`, or OpenReview forum link | Same as `[[paper]]` | `downloads/papers/` |
| Tagged `[[project]]` | Forced repo zip (GitHub or Hugging Face) | `downloads/projects/` or `downloads/hf_*/` |
| `github.com/owner/repo` | GitHub zip download | `downloads/projects/` |
| Hugging Face **model** (`huggingface.co/org/model`) | Config, tokenizer, README — weight files skipped | `downloads/hf_models/` |
| Hugging Face **dataset** (`huggingface.co/datasets/...`) | README, metadata, small data files | `downloads/hf_datasets/` |
| Hugging Face **Space** (`huggingface.co/spaces/...`) | App code, requirements, Dockerfiles | `downloads/hf_spaces/` |
| Hugging Face **file** (`/resolve/...` or `/tree/.../file`) | That file only | `downloads/files/` |
| Hugging Face **reference** (`/docs`, `/blog`, `/papers`, discussions) | Saved as HTML | `downloads/html/` |
| URL with a known file extension | Downloaded directly | `downloads/files/` |
| Anything else | Fetched once; HTML → webpage, else by `Content-Type` | `downloads/html/` or `downloads/files/` |

`[[paper]]`/`[[project]]` tags can have the URL on the same line or the
next line. Links that fail to download are left pointing at their
original URL — nothing is silently dropped.

### Hugging Face repos specifically

GitHub has a single "give me a zip" endpoint; Hugging Face doesn't, so this
tool lists files via the Hub API and zips them itself.

- **Models** always skip weight extensions (`.safetensors`, `.bin`, `.pt`, …)
  regardless of size.
- **Datasets** prefer README, metadata, and small `.csv`/`.parquet`/`.json` files.
- **Spaces** prefer app source files (`app.py`, `requirements.txt`, etc.).
- Any individual file over **200MB** is also skipped (raise with `--hf-max-file-mb`).

## Options

```bash
./run.sh notes.md ./notes_offline \
  --jobs 4 \                      # input files to process in parallel (default: all)
  --workers 8 \                   # concurrent downloads per input file
  --branch main \                 # force a branch/revision for ALL repo links
  --github-token $GITHUB_TOKEN \  # raise GitHub's rate limit (60/hr -> 5000/hr)
  --hf-max-file-mb 500 \          # raise the per-file cap for Hugging Face zips
  --timeout 30 \
  --dry-run \                     # just show classification, download nothing
  -v                               # verbose logging
```

`GITHUB_TOKEN` can also be set as an environment variable. Without one,
GitHub's unauthenticated API limit is 60 requests/hour — get a token
(no scopes needed, just a classic PAT) if your file links many repos.

## Manual install (without run.sh)

```bash
pip install -r requirements.txt
python md_downloader.py notes.md ./notes_offline
```

## Known limitations (by design, kept simple)

- Repo zip support covers github.com and huggingface.co. Other git hosts
  (GitLab, Bitbucket) aren't recognized and fall through to the generic
  file/html handling.
- PDF resolution has explicit rules for arXiv and OpenReview; other paper
  hosts are downloaded as-is, which works for any host that serves the PDF
  directly (most do) but not for hosts with a JS-gated download flow.
- HTML pages are saved as the raw server response (no JS execution) — fine
  for most docs/articles, not for JS-rendered SPAs.
