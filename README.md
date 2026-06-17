# md_downloader

Makes a markdown file fully browsable offline. Every URL in the file gets
downloaded and the link rewritten to point at the local copy — not just
`[[paper]]`/`[[project]]` tagged ones, but every plain link too.

## What happens to each URL

| URL is...                                                  | What happens                                            | Saved to              |
|--------------------------------------------------------------|-----------------------------------------------------------|------------------------|
| Tagged `[[paper]]`                                            | Forced: downloaded as a `.pdf`. arXiv `/abs/...` and OpenReview `forum?id=...` links are auto-resolved to their direct PDF URL first. | `downloads/papers/`   |
| Tagged `[[project]]`                                          | Forced: the repo downloaded as a `.zip`                   | `downloads/projects/` |
| A bare `github.com/owner/repo` or `huggingface.co/...` link (model, dataset, or space) — **no tag needed** | Auto-detected and downloaded as a `.zip` the same way      | `downloads/projects/` |
| A URL with a known file extension (`.pdf`, `.png`, `.csv`, `.py`, `.safetensors`, ...) | Downloaded directly as that file                          | `downloads/files/`    |
| Anything else                                                  | Fetched once; if the response is HTML it's saved as a webpage (with a `<base>` tag so relative images/css/js still resolve), otherwise saved as whatever it actually turned out to be, based on the real `Content-Type` header — never just guessed from the URL | `downloads/html/` or `downloads/files/` |

`[[paper]]`/`[[project]]` tags can have the URL on the same line or the
next line. Links that fail to download are left pointing at their
original URL — nothing is silently dropped.

### Hugging Face repos specifically

GitHub has a single "give me a zip" endpoint; Hugging Face doesn't, so this
tool lists every file via the Hub API and zips them itself. To avoid a
"download model repo" link accidentally pulling down tens of GB of weights,
any individual file over **200MB** is skipped by default (the zip still
gets the actual source: README, configs, tokenizer files, small assets).
Raise or remove the cap with `--hf-max-file-mb`.

## Quick start

```bash
chmod +x run.sh        # only needed once
./run.sh notes.md ./notes_offline
```

`run.sh` creates an isolated `.venv/` on first run and installs dependencies
into it automatically. Later runs reuse it and skip straight to execution.

## What you get

```
notes_offline/
├── notes.md              <- local copy, every URL replaced with a local path
├── manifest.json          <- status of every link (ok/error, content-type, etc.)
└── downloads/
    ├── papers/             <- [[paper]] links, as .pdf
    ├── projects/           <- repo links (github/huggingface), as .zip
    ├── files/              <- anything that turned out to be a file
    └── html/               <- anything that turned out to be a webpage
```

## Options

```bash
./run.sh notes.md ./notes_offline \
  --workers 8 \                   # concurrent downloads
  --branch main \                 # force a branch/revision for ALL repo links
  --github-token $GITHUB_TOKEN \  # raise GitHub's rate limit (60/hr -> 5000/hr)
  --hf-max-file-mb 500 \          # raise the per-file cap for Hugging Face zips
  --timeout 30 \
  --dry-run \                     # just show classification, download nothing (output_dir not required)
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
