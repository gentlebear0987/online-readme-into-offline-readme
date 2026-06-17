#!/usr/bin/env python3
"""
md_downloader.py

Scans a markdown file for [[paper]] / [[project]] tags. The URL that
follows each tag is downloaded:

    [[paper]]   -> the linked document, saved as a .pdf
                   (arXiv /abs/ links and OpenReview /forum links are
                   automatically resolved to their direct PDF URL first)
    [[project]] -> the linked GitHub repo, saved as a .zip

Any other plain markdown links ([text](url) or <url>) elsewhere in the
file are still downloaded too, classified generically (github repo / known
file extension / webpage HTML) -- so nothing in the file is ignored.

A local copy of the markdown is then written with every URL replaced by
the path to its local download, so you can open that file and every link
opens something on disk -- no internet required.

Usage:
    python md_downloader.py notes.md ./notes_offline
    python md_downloader.py notes.md ./notes_offline --dry-run
    GITHUB_TOKEN=ghp_xxx python md_downloader.py notes.md ./notes_offline   # higher GitHub rate limit

Result:
    notes_offline/
        notes.md              <- local copy, links rewritten
        manifest.json          <- per-link status (ok/error)
        downloads/
            papers/             <- [[paper]] links, as .pdf
            projects/           <- [[project]] links, as .zip
            repos/              <- any other bare github.com repo links, as .zip
            files/              <- any other links with a known file extension
            html/               <- any other links (saved webpages)
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

# [[paper]] or [[project]] (singular/plural, case-insensitive)
TAG_RE = re.compile(r"\[\[\s*(?P<tag>papers?|projects?)\s*\]\]", re.IGNORECASE)

# A bare URL, used to find "the next URL after a tag"
URL_RE = re.compile(r"https?://[^\s\)>\]]+")

# Generic fallback: [text](url) / ![alt](url)
MD_LINK_RE = re.compile(
    r'(?P<bang>!?)\[(?P<text>[^\]]*)\]\((?P<url>[^)\s]+)(?P<title>\s+"[^"]*")?\)'
)
# Generic fallback: <https://example.com>
AUTOLINK_RE = re.compile(r"<(?P<url>https?://[^>\s]+)>")

GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"(?:/(?:tree|commits)/(?P<branch>[^/?#]+)/?)?$"
)
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
}

DEFAULT_HEADERS = {
    "User-Agent": "md-downloader/1.0 (+https://github.com)",
}

MAX_TAG_SEARCH_WINDOW = 500  # chars to look ahead of a tag for its URL


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class LinkItem:
    url: str
    kind: str = ""              # paper | project_repo | github_repo | file | html | skip
    local_path: Path | None = None
    status: str = "pending"     # pending | ok | error
    error: str = ""
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# [[paper]] / [[project]] extraction
# --------------------------------------------------------------------------

def find_tag_links(md_text: str) -> list[dict]:
    """Find [[paper]]/[[project]] tags and the URL that follows each one.

    Returns a list of {tag, url, span} dicts, span = (start, end) of the
    URL substring's position in md_text (for precise in-place rewriting).
    """
    tag_matches = list(TAG_RE.finditer(md_text))
    results = []
    for idx, tm in enumerate(tag_matches):
        window_end = tag_matches[idx + 1].start() if idx + 1 < len(tag_matches) else len(md_text)
        window_end = min(window_end, tm.end() + MAX_TAG_SEARCH_WINDOW)
        # Don't reach across a paragraph break looking for the URL.
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
        kind = "paper" if tag.startswith("paper") else "project_repo"
        results.append({"kind": kind, "url": url, "span": (abs_start, abs_end)})
    return results


def find_generic_links(md_text: str, exclude_urls: set[str]) -> list[dict]:
    """Plain [text](url) / <url> links, skipping anything already tag-handled."""
    results = []
    for m in MD_LINK_RE.finditer(md_text):
        raw = m.group("url")
        url = raw.strip("<>")
        if url in exclude_urls:
            continue
        s, e = m.span("url")
        if raw.startswith("<") and raw.endswith(">"):
            s, e = s + 1, e - 1
        results.append({"kind": None, "url": url, "span": (s, e)})
    for m in AUTOLINK_RE.finditer(md_text):
        url = m.group("url")
        if url in exclude_urls:
            continue
        results.append({"kind": None, "url": url, "span": m.span("url")})
    return results


def classify_generic(url: str) -> tuple[str, dict]:
    """Classify a plain (non-tagged) link: github_repo | file | html | skip."""
    if not url.startswith(("http://", "https://")):
        return "skip", {}

    m = GITHUB_REPO_RE.match(url)
    if m:
        return "github_repo", {"owner": m.group("owner"), "repo": m.group("repo"), "branch": m.group("branch")}

    m = GITHUB_BLOB_RE.match(url)
    if m:
        raw_url = (
            f"https://raw.githubusercontent.com/{m.group('owner')}/"
            f"{m.group('repo')}/{m.group('branch')}/{m.group('path')}"
        )
        return "file", {"raw_url": raw_url}

    ext = Path(urlsplit(url).path).suffix.lower()
    if ext in FILE_EXTENSIONS:
        return "file", {}

    return "html", {}


def resolve_pdf_url(url: str) -> str:
    """Turn an arXiv/OpenReview landing-page URL into its direct PDF URL."""
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
                 forced_branch: str | None = None):
        self.out_dir = out_dir
        self.github_token = github_token
        self.timeout = timeout
        self.forced_branch = forced_branch
        for sub in ("papers", "projects", "repos", "files", "html"):
            (out_dir / sub).mkdir(parents=True, exist_ok=True)

    # ---- [[paper]] -> pdf -------------------------------------------------
    def download_paper(self, item: LinkItem) -> None:
        resolved = resolve_pdf_url(item.url)
        headers = dict(DEFAULT_HEADERS)
        with requests.get(resolved, headers=headers, stream=True,
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
                "  -> %s did not look like a real PDF (content-type=%s); "
                "saved as %s anyway, please check it manually",
                item.url, content_type or "unknown", dest.name,
            )

    # ---- [[project]] -> github zip ----------------------------------------
    def download_project(self, item: LinkItem) -> None:
        parsed = urlsplit(item.url)
        if "github.com" not in parsed.netloc.lower():
            raise ValueError(f"Unsupported git host for [[project]] link (only github.com is supported): {item.url}")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError(f"Could not parse owner/repo from [[project]] link: {item.url}")
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])

        branch = self.forced_branch
        if not branch and len(parts) >= 4 and parts[2] in ("tree", "commits"):
            branch = parts[3]
        if not branch:
            branch = self._default_branch(owner, repo)

        zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
        filename = sanitize(f"{owner}__{repo}__{branch}.zip")
        dest = unique_path(self.out_dir / "projects", filename)
        self._stream_to_file(zip_url, dest)
        item.local_path = dest
        item.meta["branch"] = branch

    # ---- generic fallback: bare github repo link -> zip --------------------
    def download_github_repo(self, item: LinkItem) -> None:
        owner, repo = item.meta["owner"], item.meta["repo"]
        branch = self.forced_branch or item.meta.get("branch")
        if not branch:
            branch = self._default_branch(owner, repo)
        zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
        filename = sanitize(f"{owner}__{repo}__{branch}.zip")
        dest = unique_path(self.out_dir / "repos", filename)
        self._stream_to_file(zip_url, dest)
        item.local_path = dest
        item.meta["branch"] = branch

    # ---- generic fallback: direct file -------------------------------------
    def download_file(self, item: LinkItem) -> None:
        url = item.meta.get("raw_url", item.url)
        base = os.path.basename(urlsplit(url).path) or short_hash(url)
        filename = sanitize(base)
        if "." not in filename:
            filename += ".bin"
        dest = unique_path(self.out_dir / "files", filename)
        self._stream_to_file(url, dest)
        item.local_path = dest

    # ---- generic fallback: webpage -> html ----------------------------------
    def download_html(self, item: LinkItem) -> None:
        headers = dict(DEFAULT_HEADERS)
        headers["Accept"] = "text/html,application/xhtml+xml"
        r = requests.get(item.url, headers=headers, timeout=self.timeout)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        if soup.head is None:
            head = soup.new_tag("head")
            if soup.html:
                soup.html.insert(0, head)
            else:
                soup.insert(0, head)
        soup.head.insert(0, soup.new_tag("base", href=item.url))

        parsed = urlsplit(item.url)
        slug = sanitize(f"{parsed.netloc}{parsed.path}") or "page"
        filename = f"{slug}_{short_hash(item.url)}.html"
        dest = unique_path(self.out_dir / "html", filename)
        dest.write_text(str(soup), encoding="utf-8")
        item.local_path = dest

    # ---- shared helpers -----------------------------------------------------
    def _default_branch(self, owner: str, repo: str) -> str:
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
            elif item.kind == "project_repo":
                self.download_project(item)
            elif item.kind == "github_repo":
                self.download_github_repo(item)
            elif item.kind == "file":
                self.download_file(item)
            elif item.kind == "html":
                self.download_html(item)
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
    # Apply right-to-left so earlier spans' positions stay valid.
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
                         help="Force this branch for ALL github repo/[[project]] links")
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

    tag_links = find_tag_links(md_text)
    tag_urls = {lk["url"] for lk in tag_links}
    generic_links = find_generic_links(md_text, exclude_urls=tag_urls)
    all_links = tag_links + generic_links

    # Build the de-duplicated download list (one item per unique URL).
    items_by_url: dict[str, LinkItem] = {}
    for lk in tag_links:
        if lk["url"] not in items_by_url:
            items_by_url[lk["url"]] = LinkItem(url=lk["url"], kind=lk["kind"])
    for lk in generic_links:
        if lk["url"] not in items_by_url:
            kind, meta = classify_generic(lk["url"])
            items_by_url[lk["url"]] = LinkItem(url=lk["url"], kind=kind, meta=meta)
    items = list(items_by_url.values())

    if not items:
        log.info("No links found in %s (looking for [[paper]]/[[project]] tags and plain markdown links)", md_path)
        return 0

    counts = Counter(i.kind for i in items)
    log.info("Found %d link(s): %s", len(items), ", ".join(f"{v} {k}" for k, v in counts.items()))

    if args.dry_run:
        for i in items:
            print(f"  [{i.kind:13s}] {i.url}")
        return 0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = out_dir / "downloads"
    downloader = Downloader(downloads_dir, args.github_token, args.timeout, args.branch)

    downloadable = [i for i in items if i.kind != "skip"]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(downloader.run, item): item for item in downloadable}
        for _ in as_completed(futures):
            pass  # progress already logged inside Downloader.run

    output_md = out_dir / md_path.name
    url_to_rel: dict[str, str] = {}
    manifest = {}
    for item in items:
        entry = {"kind": item.kind, "status": item.status, "error": item.error}
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
