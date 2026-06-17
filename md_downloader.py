#!/usr/bin/env python3
"""
md_downloader.py

Downloads EVERY url in a markdown file so the file can be browsed fully
offline, deciding what to do with each one:

    [[paper]]   tag  -> forced: download as a .pdf (arXiv/OpenReview
                        landing pages are resolved to their direct PDF URL)
    [[project]] tag  -> forced: download the repo (github.com or
                        huggingface.co) as a .zip
    any bare github.com or huggingface.co repo link (no tag needed)
                      -> also downloaded as a .zip, same as [[project]]
    a URL with a known file extension (.pdf, .png, .csv, .py, ...)
                      -> downloaded as that file
    anything else     -> fetched once; if the response is HTML it's saved
                        as a webpage (with a <base> tag so relative
                        images/css/js still resolve), otherwise it's
                        saved as whatever file it actually is, based on
                        the real Content-Type -- not a guess from the URL

A local copy of the markdown is then written with every URL replaced by
the path to its local download.

Usage:
    python md_downloader.py notes.md ./notes_offline
    python md_downloader.py notes.md ./notes_offline --dry-run
    GITHUB_TOKEN=ghp_xxx python md_downloader.py notes.md ./notes_offline

Result:
    notes_offline/
        notes.md              <- local copy, links rewritten
        manifest.json          <- per-link status (ok/error/content-type)
        downloads/
            papers/             <- [[paper]] links, as .pdf
            projects/           <- repo links (github/huggingface), as .zip
            files/              <- anything that turned out to be a file
            html/               <- anything that turned out to be a webpage
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("md_downloader")

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"\[\[\s*(?P<tag>papers?|projects?)\s*\]\]", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\)>\]]+")

MD_LINK_RE = re.compile(
    r'(?P<bang>!?)\[(?P<text>[^\]]*)\]\((?P<url>[^)\s]+)(?P<title>\s+"[^"]*")?\)'
)
AUTOLINK_RE = re.compile(r"<(?P<url>https?://[^>\s]+)>")

GITHUB_BLOB_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/blob/"
    r"(?P<branch>[^/]+)/(?P<path>[^?#]+)"
)

FILE_EXTENSIONS = {
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv",
    ".csv", ".json", ".xml", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".txt", ".rst", ".log",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs", ".sh",
    ".whl", ".jar", ".exe", ".dmg", ".deb", ".apk", ".gitignore",
    ".safetensors", ".bin", ".onnx", ".pt", ".ckpt", ".h5", ".parquet",
}

HF_NON_REPO_TOP_LEVEL = {
    "models", "datasets", "spaces", "blog", "docs", "papers", "learn",
    "course", "pricing", "join", "login", "settings", "about", "tasks",
    "search", "collections", "posts", "organizations",
}
HF_STOP_KEYWORDS = {"tree", "blob", "resolve", "commit", "commits", "discussions", "raw", "blame", "edit"}

DEFAULT_HEADERS = {
    "User-Agent": "md-downloader/1.0 (+https://github.com)",
}

HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
MAX_TAG_SEARCH_WINDOW = 500


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class LinkItem:
    url: str
    kind: str = ""              # paper | project | repo | file | auto | skip
    local_path: Path | None = None
    status: str = "pending"     # pending | ok | error
    error: str = ""
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# [[paper]] / [[project]] extraction
# --------------------------------------------------------------------------

def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def find_tag_links(md_text: str) -> list[dict]:
    tag_matches = list(TAG_RE.finditer(md_text))
    results = []
    for idx, tm in enumerate(tag_matches):
        window_end = tag_matches[idx + 1].start() if idx + 1 < len(tag_matches) else len(md_text)
        window_end = min(window_end, tm.end() + MAX_TAG_SEARCH_WINDOW)
        para_break = re.search(r"\n\s*\n", md_text[tm.end():window_end])
        if para_break:
            window_end = tm.end() + para_break.start()

        window_text = md_text[tm.end():window_end]
        um = URL_RE.search(window_text)
        if not um:
            log.warning("No URL found after %s tag near position %d", tm.group(0), tm.start())
            continue

        url = um.group(0).rstrip(".,;:)]>")
        abs_start = tm.end() + um.start()
        abs_end = abs_start + len(url)
        tag = tm.group("tag").lower()
        kind = "paper" if tag.startswith("paper") else "project"
        results.append({"kind": kind, "url": url, "span": (abs_start, abs_end)})
    return results


def extract_all_links(md_text: str) -> list[dict]:
    """Single overlap-aware pass: [[tag]] urls, then [text](url)/<url>, then any
    remaining bare https?:// URL in the text. Every URL in the file is returned
    exactly once, each with a non-overlapping span for safe in-place rewriting.
    """
    results: list[dict] = []
    consumed: list[tuple[int, int]] = []

    for lk in find_tag_links(md_text):
        results.append(lk)
        consumed.append(lk["span"])

    for m in MD_LINK_RE.finditer(md_text):
        raw = m.group("url")
        url = raw.strip("<>")
        if not url.startswith(("http://", "https://")):
            continue
        s, e = m.span("url")
        if raw.startswith("<") and raw.endswith(">"):
            s, e = s + 1, e - 1
        if any(_spans_overlap((s, e), c) for c in consumed):
            continue
        results.append({"kind": None, "url": url, "span": (s, e)})
        consumed.append((s, e))

    for m in AUTOLINK_RE.finditer(md_text):
        s, e = m.span("url")
        if any(_spans_overlap((s, e), c) for c in consumed):
            continue
        results.append({"kind": None, "url": m.group("url"), "span": (s, e)})
        consumed.append((s, e))

    for m in URL_RE.finditer(md_text):
        url = m.group(0).rstrip(".,;:)]>")
        s = m.start()
        e = s + len(url)
        if any(_spans_overlap((s, e), c) for c in consumed):
            continue
        results.append({"kind": None, "url": url, "span": (s, e)})
        consumed.append((s, e))

    return results


# --------------------------------------------------------------------------
# Repo host detection (github.com / huggingface.co)
# --------------------------------------------------------------------------

def parse_hf_repo(url: str) -> dict | None:
    """Parse a huggingface.co URL into repo_type/api_prefix/url_prefix/repo_id/revision."""
    parsed = urlsplit(url)
    if parsed.netloc.lower() not in ("huggingface.co", "www.huggingface.co"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None

    repo_type, api_prefix, url_prefix = "model", "models", ""
    if parts[0] == "datasets":
        repo_type, api_prefix, url_prefix = "dataset", "datasets", "datasets/"
        parts = parts[1:]
    elif parts[0] == "spaces":
        repo_type, api_prefix, url_prefix = "space", "spaces", "spaces/"
        parts = parts[1:]

    if not parts or parts[0] in HF_NON_REPO_TOP_LEVEL:
        return None

    id_parts, revision = [], None
    for i, p in enumerate(parts):
        if p in HF_STOP_KEYWORDS:
            if i + 1 < len(parts):
                revision = parts[i + 1]
            break
        id_parts.append(p)
    if not id_parts:
        return None

    return {
        "repo_type": repo_type, "api_prefix": api_prefix, "url_prefix": url_prefix,
        "repo_id": "/".join(id_parts), "revision": revision,
    }


def detect_repo_host(url: str) -> str | None:
    """Return 'github' / 'huggingface' if url looks like a repo root, else None."""
    parsed = urlsplit(url)
    netloc = parsed.netloc.lower()
    if netloc in ("github.com", "www.github.com"):
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2:
            return "github"
        return None
    if parse_hf_repo(url) is not None:
        return "huggingface"
    return None


def classify_generic(url: str) -> tuple[str, dict]:
    """skip | file | repo | auto -- for any URL not under a [[paper]]/[[project]] tag."""
    if not url.startswith(("http://", "https://")):
        return "skip", {}

    m = GITHUB_BLOB_RE.match(url)
    if m:
        raw_url = (
            f"https://raw.githubusercontent.com/{m.group('owner')}/"
            f"{m.group('repo')}/{m.group('branch')}/{m.group('path')}"
        )
        return "file", {"raw_url": raw_url}

    if detect_repo_host(url):
        return "repo", {}

    ext = Path(urlsplit(url).path).suffix.lower()
    if ext in FILE_EXTENSIONS:
        return "file", {}

    return "auto", {}


def resolve_pdf_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path

    if host.endswith("arxiv.org"):
        if path.startswith("/abs/"):
            new_path = "/pdf/" + path[len("/abs/"):]
            if not new_path.endswith(".pdf"):
                new_path += ".pdf"
            return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))
        if path.startswith("/pdf/") and not path.endswith(".pdf"):
            return urlunsplit((parts.scheme, parts.netloc, path + ".pdf", parts.query, parts.fragment))
        return url

    if host.endswith("openreview.net") and path.startswith("/forum"):
        return urlunsplit((parts.scheme, parts.netloc, "/pdf", parts.query, parts.fragment))

    return url


# --------------------------------------------------------------------------
# Filesystem helpers
# --------------------------------------------------------------------------

def short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def sanitize(name: str, max_len: int = 80) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return (name or "file")[:max_len]


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    h = short_hash(filename + str(os.urandom(4)))
    return directory / f"{stem}_{h}{suffix}"


def derive_slug(url: str) -> str:
    parts = urlsplit(url)
    if parts.query:
        qs = parse_qs(parts.query)
        if qs.get("id"):
            return sanitize(qs["id"][0])
    stem = Path(parts.path).stem
    return sanitize(stem) if stem else "paper"


# --------------------------------------------------------------------------
# Downloader
# --------------------------------------------------------------------------

class Downloader:
    def __init__(self, out_dir: Path, github_token: str | None, timeout: int,
                 forced_branch: str | None = None, hf_max_file_mb: int = 200):
        self.out_dir = out_dir
        self.github_token = github_token
        self.timeout = timeout
        self.forced_branch = forced_branch
        self.hf_max_file_bytes = hf_max_file_mb * 1024 * 1024
        self.hf_max_file_mb = hf_max_file_mb
        for sub in ("papers", "projects", "files", "html"):
            (out_dir / sub).mkdir(parents=True, exist_ok=True)

    # ---- [[paper]] -> pdf ---------------------------------------------------
    def download_paper(self, item: LinkItem) -> None:
        resolved = resolve_pdf_url(item.url)
        with requests.get(resolved, headers=DEFAULT_HEADERS, stream=True,
                           timeout=self.timeout, allow_redirects=True) as r:
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            looks_like_pdf = content_type == "application/pdf" or resolved.lower().endswith(".pdf")
            ext = ".pdf" if looks_like_pdf else (mimetypes.guess_extension(content_type) or ".bin")
            filename = f"{derive_slug(resolved)}_{short_hash(item.url)}{ext}"
            dest = unique_path(self.out_dir / "papers", filename)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        item.local_path = dest
        item.meta["resolved_url"] = resolved
        item.meta["looks_like_pdf"] = looks_like_pdf
        if not looks_like_pdf:
            log.warning(
                "  -> %s did not look like a real PDF (content-type=%s); saved as %s anyway, please check it manually",
                item.url, content_type or "unknown", dest.name,
            )

    # ---- [[project]] tag or bare repo link -> zip ---------------------------
    def download_repo(self, item: LinkItem) -> None:
        host = detect_repo_host(item.url)
        if host == "github":
            self._download_github_zip(item)
        elif host == "huggingface":
            self._download_hf_zip(item)
        else:
            raise ValueError(f"Unrecognized repo host (only github.com and huggingface.co are supported): {item.url}")

    def _download_github_zip(self, item: LinkItem) -> None:
        parsed = urlsplit(item.url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError(f"Could not parse owner/repo from github link: {item.url}")
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])

        branch = self.forced_branch
        if not branch and len(parts) >= 4 and parts[2] in ("tree", "commits"):
            branch = parts[3]
        if not branch:
            branch = self._github_default_branch(owner, repo)

        zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
        filename = sanitize(f"{owner}__{repo}__{branch}.zip")
        dest = unique_path(self.out_dir / "projects", filename)
        self._stream_to_file(zip_url, dest)
        item.local_path = dest
        item.meta["branch"] = branch

    def _download_hf_zip(self, item: LinkItem) -> None:
        info = parse_hf_repo(item.url)
        if info is None:
            raise ValueError(f"Could not parse a Hugging Face repo id from: {item.url}")

        revision = self.forced_branch or info["revision"] or "main"
        api_url = f"https://huggingface.co/api/{info['api_prefix']}/{info['repo_id']}"
        if revision != "main":
            api_url += f"/revision/{revision}"
        r = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=self.timeout)
        r.raise_for_status()
        siblings = r.json().get("siblings", [])
        if not siblings:
            raise ValueError(f"No files listed for Hugging Face repo {info['repo_id']} (gated, private, or empty?)")

        included, skipped = [], []
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for sib in siblings:
                rfilename = sib.get("rfilename")
                if not rfilename:
                    continue
                file_url = f"https://huggingface.co/{info['url_prefix']}{info['repo_id']}/resolve/{revision}/{rfilename}"
                try:
                    with requests.get(file_url, headers=DEFAULT_HEADERS, stream=True,
                                       timeout=self.timeout, allow_redirects=True) as fr:
                        fr.raise_for_status()
                        size = int(fr.headers.get("Content-Length") or 0)
                        if size > self.hf_max_file_bytes:
                            skipped.append(rfilename)
                            continue
                        dest_file = tmp_path / rfilename
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        with open(dest_file, "wb") as f:
                            for chunk in fr.iter_content(chunk_size=1 << 16):
                                if chunk:
                                    f.write(chunk)
                        included.append(rfilename)
                except requests.RequestException as exc:
                    log.warning("  -> failed to fetch %s from %s (%s), skipping that file", rfilename, info["repo_id"], exc)
                    skipped.append(rfilename)

            if not included:
                raise ValueError(f"All files in {info['repo_id']} were skipped (too large or failed) -- nothing to zip")

            safe_name = sanitize(info["repo_id"].replace("/", "__"))
            filename = f"{safe_name}__{revision}__hf.zip"
            dest = unique_path(self.out_dir / "projects", filename)
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel in included:
                    zf.write(tmp_path / rel, arcname=rel)

        item.local_path = dest
        item.meta["revision"] = revision
        item.meta["files_included"] = len(included)
        item.meta["files_skipped"] = len(skipped)
        if skipped:
            log.info(
                "  -> %s: zipped %d file(s), skipped %d over %dMB cap (--hf-max-file-mb to change), e.g. %s",
                item.url, len(included), len(skipped), self.hf_max_file_mb, skipped[0],
            )

    # ---- known-extension file -------------------------------------------------
    def download_file(self, item: LinkItem) -> None:
        url = item.meta.get("raw_url", item.url)
        base = os.path.basename(urlsplit(url).path) or short_hash(url)
        filename = sanitize(base)
        if "." not in filename:
            filename += ".bin"
        dest = unique_path(self.out_dir / "files", filename)
        self._stream_to_file(url, dest)
        item.local_path = dest

    # ---- "auto": sniff Content-Type once, decide file vs html -----------------
    def download_auto(self, item: LinkItem) -> None:
        headers = dict(DEFAULT_HEADERS)
        headers["Accept"] = "*/*"
        with requests.get(item.url, headers=headers, stream=True,
                          timeout=self.timeout, allow_redirects=True) as r:
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            item.meta["content_type"] = content_type or "unknown"

            if content_type in HTML_CONTENT_TYPES:
                dest = self._save_html(item.url, r.text)
                item.kind = "html"
            else:
                ext = mimetypes.guess_extension(content_type) if content_type else None
                if not ext:
                    ext = Path(urlsplit(item.url).path).suffix or ".bin"
                base = os.path.basename(urlsplit(item.url).path) or short_hash(item.url)
                stem = sanitize(Path(base).stem or "file")
                filename = f"{stem}{ext}"
                dest = unique_path(self.out_dir / "files", filename)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                item.kind = "file"
        item.local_path = dest

    # ---- shared helpers ---------------------------------------------------------
    def _save_html(self, url: str, html_text: str) -> Path:
        soup = BeautifulSoup(html_text, "html.parser")
        if soup.head is None:
            head = soup.new_tag("head")
            if soup.html:
                soup.html.insert(0, head)
            else:
                soup.insert(0, head)
        soup.head.insert(0, soup.new_tag("base", href=url))

        parsed = urlsplit(url)
        slug = sanitize(f"{parsed.netloc}{parsed.path}") or "page"
        filename = f"{slug}_{short_hash(url)}.html"
        dest = unique_path(self.out_dir / "html", filename)
        dest.write_text(str(soup), encoding="utf-8")
        return dest

    def _github_default_branch(self, owner: str, repo: str) -> str:
        headers = dict(DEFAULT_HEADERS)
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["default_branch"]

    def _stream_to_file(self, url: str, dest: Path) -> None:
        headers = dict(DEFAULT_HEADERS)
        if ("codeload.github.com" in url or "api.github.com" in url) and self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        with requests.get(url, headers=headers, stream=True, timeout=self.timeout) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)

    def run(self, item: LinkItem) -> LinkItem:
        try:
            if item.kind == "paper":
                self.download_paper(item)
            elif item.kind in ("project", "repo"):
                self.download_repo(item)
            elif item.kind == "file":
                self.download_file(item)
            elif item.kind == "auto":
                self.download_auto(item)
            item.status = "ok"
            log.info("OK   [%s] %s -> %s", item.kind, item.url, item.local_path)
        except Exception as exc:  # noqa: BLE001
            item.status = "error"
            item.error = str(exc)
            log.warning("FAIL [%s] %s (%s)", item.kind, item.url, exc)
        return item


# --------------------------------------------------------------------------
# Markdown rewriting (span-based: handles tag links + generic links uniformly)
# --------------------------------------------------------------------------

def rewrite_markdown(md_text: str, all_links: list[dict], url_to_rel: dict[str, str]) -> str:
    replacements = [
        (lk["span"][0], lk["span"][1], url_to_rel[lk["url"]])
        for lk in all_links if lk["url"] in url_to_rel
    ]
    replacements.sort(key=lambda t: t[0], reverse=True)
    text = md_text
    for start, end, replacement in replacements:
        text = text[:start] + replacement + text[end:]
    return text


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("md_file", help="Path to the input markdown file")
    parser.add_argument("output_dir", nargs="?", default=None,
                         help="New directory to create. Will contain a local copy of the "
                              "markdown file (links rewritten to point locally) plus a "
                              "downloads/ folder with the actual downloaded content. "
                              "Not required with --dry-run.")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent downloads")
    parser.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"),
                         help="GitHub token to raise API/rate limits (or set GITHUB_TOKEN env var)")
    parser.add_argument("--branch", default=None,
                         help="Force this branch/revision for ALL repo links (github and huggingface)")
    parser.add_argument("--hf-max-file-mb", type=int, default=200,
                         help="Skip individual files in a Hugging Face repo larger than this "
                              "many MB (e.g. model weights) when building its zip. Default 200.")
    parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout (s)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Only show classification, don't download anything")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    md_path = Path(args.md_file)
    if not md_path.exists():
        log.error("File not found: %s", md_path)
        return 1
    if not args.dry_run and not args.output_dir:
        log.error("output_dir is required (only optional together with --dry-run)")
        return 1
    md_text = md_path.read_text(encoding="utf-8")

    all_links = extract_all_links(md_text)

    items_by_url: dict[str, LinkItem] = {}
    for lk in all_links:
        if lk["url"] in items_by_url:
            continue
        if lk["kind"] is not None:
            items_by_url[lk["url"]] = LinkItem(url=lk["url"], kind=lk["kind"])
        else:
            kind, meta = classify_generic(lk["url"])
            items_by_url[lk["url"]] = LinkItem(url=lk["url"], kind=kind, meta=meta)
    items = list(items_by_url.values())

    if not items:
        log.info("No links found in %s", md_path)
        return 0

    counts = Counter(i.kind for i in items)
    log.info("Found %d link(s): %s", len(items), ", ".join(f"{v} {k}" for k, v in counts.items()))

    if args.dry_run:
        for i in items:
            print(f"  [{i.kind:9s}] {i.url}")
        return 0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = out_dir / "downloads"
    downloader = Downloader(downloads_dir, args.github_token, args.timeout, args.branch, args.hf_max_file_mb)

    downloadable = [i for i in items if i.kind != "skip"]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(downloader.run, item): item for item in downloadable}
        for _ in as_completed(futures):
            pass

    output_md = out_dir / md_path.name
    url_to_rel: dict[str, str] = {}
    manifest = {}
    for item in items:
        entry = {"kind": item.kind, "status": item.status, "error": item.error, **{
            k: v for k, v in item.meta.items() if isinstance(v, (str, int, float, bool))
        }}
        if item.local_path is not None:
            rel = Path(os.path.relpath(item.local_path, start=output_md.parent)).as_posix()
            url_to_rel[item.url] = rel
            entry["local_path"] = rel
        manifest[item.url] = entry

    new_md_text = rewrite_markdown(md_text, all_links, url_to_rel)
    output_md.write_text(new_md_text, encoding="utf-8")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    ok = sum(1 for i in items if i.status == "ok")
    err = sum(1 for i in items if i.status == "error")
    log.info("\nDone. %d downloaded, %d failed.", ok, err)
    log.info("Project dir         -> %s", out_dir)
    log.info("Local markdown      -> %s", output_md)
    log.info("Downloaded content  -> %s", downloads_dir)
    log.info("Manifest            -> %s", manifest_path)
    return 0 if err == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
