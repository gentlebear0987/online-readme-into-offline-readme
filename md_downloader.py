#!/usr/bin/env python3
"""
md_downloader.py

Downloads EVERY url in a markdown file so the file can be browsed fully
offline, deciding what to do with each one:

    [[paper]]   tag  -> forced: download as a .pdf (arXiv/OpenReview
                        landing pages are resolved to their direct PDF URL)
    [[project]] tag  -> forced: download the repo (github.com or
                        huggingface.co) as a .zip
    bare arXiv / OpenReview links (no tag) -> same PDF handling as [[paper]]
    any bare github.com repo link (no tag needed) -> downloaded as a .zip
    huggingface.co links -> classified by type:
        model   -> config/tokenizer/README zip (weights skipped)
        dataset -> metadata and small data files
        space   -> app source code zip
        file    -> single file via /resolve/ or /tree/.../file
        reference (docs/blog/papers) -> saved as HTML
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
    python md_downloader.py notes.md                    # writes ../<parent>_1.zip
    python md_downloader.py "a.md, b.zip, c"            # comma-separated, quote-aware
    python md_downloader.py notes.md ./notes_offline --dry-run
    GITHUB_TOKEN=ghp_xxx python md_downloader.py notes.md ./notes_offline

Result:
    notes_offline/
        notes.md              <- local copy, links rewritten
        manifest.json          <- per-link status (ok/error/content-type)
        downloads/
            papers/             <- PDFs
            projects/           <- GitHub repo zips
            hf_models/          <- Hugging Face models
            hf_datasets/        <- Hugging Face datasets
            hf_spaces/          <- Hugging Face Spaces
            files/              <- direct file downloads
            html/               <- webpages
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
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
    "search", "collections", "posts", "organizations", "enterprise", "changelog",
}
HF_STOP_KEYWORDS = {"tree", "blob", "resolve", "commit", "commits", "discussions", "raw", "blame", "edit"}
HF_FILE_KEYWORDS = {"resolve", "raw"}
HF_NAV_KEYWORDS = {"tree", "blob", "commits", "commit", "discussions", "discussion", "blame", "edit"}

HF_MODEL_SKIP_EXT = {".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".h5", ".onnx", ".msgpack", ".gguf"}
HF_DATASET_PREFER_EXT = {".md", ".json", ".py", ".csv", ".parquet", ".txt", ".yaml", ".yml", ".tsv", ".arrow"}
HF_SPACE_PREFER_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".txt", ".json",
    ".yaml", ".yml", ".toml", ".sh", ".ipynb", ".dockerfile", ".gradle", ".java",
}

HF_CATEGORY_DIRS = {
    "model": "hf_models",
    "dataset": "hf_datasets",
    "space": "hf_spaces",
}

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


@dataclass
class ResolvedInput:
    md_path: Path
    source_zip: Path | None = None
    cleanup_dirs: list[Path] = field(default_factory=list)

    def output_basename(self) -> str:
        if self.source_zip is not None:
            return self.source_zip.stem
        return self.md_path.parent.name


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

def _hf_host(url: str) -> bool:
    return urlsplit(url).netloc.lower() in ("huggingface.co", "www.huggingface.co")


def _hf_repo_prefix(parts: list[str]) -> tuple[str, str, str, int]:
    """Return (repo_type, api_prefix, url_prefix, start_index)."""
    if not parts:
        return "model", "models", "", 0
    if parts[0] == "datasets":
        return "dataset", "datasets", "datasets/", 1
    if parts[0] == "spaces":
        return "space", "spaces", "spaces/", 1
    return "model", "models", "", 0


def _hf_repo_info(repo_type: str, api_prefix: str, url_prefix: str,
                  repo_id: str, revision: str | None) -> dict:
    return {
        "repo_type": repo_type,
        "api_prefix": api_prefix,
        "url_prefix": url_prefix,
        "repo_id": repo_id,
        "revision": revision,
    }


def classify_hf_url(url: str) -> tuple[str, dict] | None:
    """Classify a huggingface.co URL.

    Returns (kind, meta) where kind is:
      repo      -> model / dataset / space repository (zip selected files)
      file      -> single file via /resolve/ or /tree/.../file
      reference -> docs, blog, papers, discussions, etc. (save as HTML)
    meta includes hf_category: model | dataset | space | reference
    """
    if not _hf_host(url):
        return None

    parts = [p for p in urlsplit(url).path.split("/") if p]
    if not parts:
        return "reference", {"hf_category": "reference"}

    repo_type, api_prefix, url_prefix, idx = _hf_repo_prefix(parts)
    rest = parts[idx:]

    if parts[0] in HF_NON_REPO_TOP_LEVEL:
        if parts[0] in ("datasets", "spaces") and rest:
            pass  # e.g. /datasets/squad or /spaces/org/name — repo, not a listing page
        else:
            return "reference", {"hf_category": "reference", "hf_section": parts[0]}

    if not rest:
        return "reference", {"hf_category": "reference", "hf_section": repo_type}

    for i, part in enumerate(rest):
        if part in HF_FILE_KEYWORDS:
            if i + 2 < len(rest):
                repo_id = "/".join(rest[:i])
                revision = rest[i + 1]
                file_path = "/".join(rest[i + 2:])
                raw_url = (
                    f"https://huggingface.co/{url_prefix}{repo_id}"
                    f"/resolve/{revision}/{file_path}"
                )
                return "file", {
                    "hf_category": repo_type,
                    "hf_file": file_path,
                    "raw_url": raw_url,
                    "hf_info": _hf_repo_info(repo_type, api_prefix, url_prefix, repo_id, revision),
                }
            break

        if part in ("tree", "blob"):
            if i + 1 < len(rest):
                repo_id = "/".join(rest[:i])
                revision = rest[i + 1]
                if i + 2 < len(rest):
                    file_path = "/".join(rest[i + 2:])
                    raw_url = (
                        f"https://huggingface.co/{url_prefix}{repo_id}"
                        f"/resolve/{revision}/{file_path}"
                    )
                    return "file", {
                        "hf_category": repo_type,
                        "hf_file": file_path,
                        "raw_url": raw_url,
                        "hf_info": _hf_repo_info(repo_type, api_prefix, url_prefix, repo_id, revision),
                    }
                return "repo", {
                    "hf_category": repo_type,
                    "hf_info": _hf_repo_info(repo_type, api_prefix, url_prefix, repo_id, revision),
                }
            break

        if part in ("discussions", "discussion", "commits", "commit", "blame", "edit"):
            return "reference", {"hf_category": "reference", "hf_section": part}

    id_parts: list[str] = []
    revision = None
    for i, part in enumerate(rest):
        if part in HF_STOP_KEYWORDS:
            if i + 1 < len(rest):
                revision = rest[i + 1]
            break
        id_parts.append(part)

    if id_parts:
        return "repo", {
            "hf_category": repo_type,
            "hf_info": _hf_repo_info(
                repo_type, api_prefix, url_prefix, "/".join(id_parts), revision,
            ),
        }

    return "reference", {"hf_category": "reference"}


def should_include_hf_file(filename: str, size: int, category: str, max_bytes: int) -> bool:
    if size > max_bytes:
        return False
    ext = Path(filename).suffix.lower()
    name_lower = filename.lower()
    if category == "model":
        if ext in HF_MODEL_SKIP_EXT:
            return False
        return True
    if category == "dataset":
        if ext in HF_DATASET_PREFER_EXT or name_lower in ("readme.md", "dataset_infos.json"):
            return True
        return ext not in HF_MODEL_SKIP_EXT
    if category == "space":
        if ext in HF_SPACE_PREFER_EXT or name_lower in (
            "dockerfile", "requirements.txt", "app.py", "package.json",
        ):
            return True
        return ext not in HF_MODEL_SKIP_EXT
    return True


def parse_hf_repo(url: str) -> dict | None:
    """Parse a huggingface.co repo URL (legacy helper; prefer classify_hf_url)."""
    result = classify_hf_url(url)
    if result is None or result[0] != "repo":
        return None
    return result[1].get("hf_info")


def detect_repo_host(url: str) -> str | None:
    """Return 'github' / 'huggingface' if url looks like a repo root, else None."""
    parsed = urlsplit(url)
    netloc = parsed.netloc.lower()
    if netloc in ("github.com", "www.github.com"):
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2:
            return "github"
        return None
    hf = classify_hf_url(url)
    if hf is not None and hf[0] == "repo":
        return "huggingface"
    return None


def apply_hf_classification(url: str, kind: str | None, meta: dict) -> tuple[str, dict]:
    """Merge Hugging Face-specific kind/meta into a link classification."""
    hf = classify_hf_url(url)
    if hf is None:
        return kind or "auto", meta
    hf_kind, hf_meta = hf
    merged = {**meta, **hf_meta}
    if kind == "paper":
        return kind, merged
    if hf_kind == "reference":
        return "auto", merged
    if hf_kind == "file":
        return "file", merged
    if kind in (None, "repo", "project"):
        return "repo", merged
    return kind, merged


def is_pdf_landing_page(url: str) -> bool:
    """True for arXiv abs/pdf pages and OpenReview forum links that should fetch a PDF."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path
    if host.endswith("arxiv.org") and (path.startswith("/abs/") or path.startswith("/pdf/")):
        return True
    if host.endswith("openreview.net") and path.startswith("/forum"):
        return True
    return False


def classify_generic(url: str) -> tuple[str, dict]:
    """skip | file | repo | auto -- for any URL not under a [[paper]]/[[project]] tag."""
    if not url.startswith(("http://", "https://")):
        return "skip", {}

    if is_pdf_landing_page(url):
        return "paper", {}

    m = GITHUB_BLOB_RE.match(url)
    if m:
        raw_url = (
            f"https://raw.githubusercontent.com/{m.group('owner')}/"
            f"{m.group('repo')}/{m.group('branch')}/{m.group('path')}"
        )
        return "file", {"raw_url": raw_url}

    hf = classify_hf_url(url)
    if hf is not None:
        kind, meta = hf
        if kind == "reference":
            return "auto", meta
        return kind, meta

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
        for sub in ("papers", "projects", "files", "html", *HF_CATEGORY_DIRS.values()):
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
        info = item.meta.get("hf_info") or parse_hf_repo(item.url)
        if info is None:
            raise ValueError(f"Could not parse a Hugging Face repo id from: {item.url}")

        category = item.meta.get("hf_category") or info.get("repo_type", "model")
        out_subdir = HF_CATEGORY_DIRS.get(category, "projects")

        revision = self.forced_branch or info.get("revision") or "main"
        api_url = f"https://huggingface.co/api/{info['api_prefix']}/{info['repo_id']}"
        if revision != "main":
            api_url += f"/revision/{revision}"
        r = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=self.timeout)
        r.raise_for_status()
        siblings = r.json().get("siblings", [])
        if not siblings:
            raise ValueError(
                f"No files listed for Hugging Face {category} {info['repo_id']} "
                "(gated, private, or empty?)"
            )

        included, skipped = [], []
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for sib in siblings:
                rfilename = sib.get("rfilename")
                if not rfilename:
                    continue
                file_url = (
                    f"https://huggingface.co/{info['url_prefix']}{info['repo_id']}"
                    f"/resolve/{revision}/{rfilename}"
                )
                try:
                    with requests.get(file_url, headers=DEFAULT_HEADERS, stream=True,
                                       timeout=self.timeout, allow_redirects=True) as fr:
                        fr.raise_for_status()
                        size = int(fr.headers.get("Content-Length") or 0)
                        if not should_include_hf_file(
                            rfilename, size, category, self.hf_max_file_bytes,
                        ):
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
                    log.warning(
                        "  -> failed to fetch %s from %s (%s), skipping that file",
                        rfilename, info["repo_id"], exc,
                    )
                    skipped.append(rfilename)

            if not included:
                raise ValueError(
                    f"All files in {info['repo_id']} were skipped for {category} "
                    f"(filtered or over {self.hf_max_file_mb}MB) -- nothing to zip"
                )

            safe_name = sanitize(info["repo_id"].replace("/", "__"))
            filename = f"{safe_name}__{revision}__{category}.zip"
            dest = unique_path(self.out_dir / out_subdir, filename)
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel in included:
                    zf.write(tmp_path / rel, arcname=rel)

        item.local_path = dest
        item.meta["hf_category"] = category
        item.meta["revision"] = revision
        item.meta["files_included"] = len(included)
        item.meta["files_skipped"] = len(skipped)
        if skipped:
            log.info(
                "  -> HF %s %s: zipped %d file(s), skipped %d (%s filter/size), e.g. %s",
                category, item.url, len(included), len(skipped), category, skipped[0],
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
# Input parsing and path resolution
# --------------------------------------------------------------------------

def split_input_tokens(raw: str) -> list[str]:
    """Split on comma (optional following space), respecting single/double quotes."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in "'\"":
            quote = ch
        elif ch == ",":
            tok = "".join(current).strip()
            if tok:
                tokens.append(tok)
            current = []
            if i + 1 < len(raw) and raw[i + 1] == " ":
                i += 1
        else:
            current.append(ch)
        i += 1
    tok = "".join(current).strip()
    if tok:
        tokens.append(tok)
    return tokens


def find_md_in_folder(folder: Path) -> Path:
    folder = folder.resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"Directory not found: {folder}")

    def try_folder(target: Path) -> Path | None:
        for name in ("README.md", "readme.md", "Readme.md"):
            candidate = target / name
            if candidate.is_file():
                return candidate
        mds = sorted(target.glob("*.md"))
        if len(mds) == 1:
            return mds[0]
        return None

    found = try_folder(folder)
    if found is not None:
        return found

    children = [p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(children) == 1:
        found = try_folder(children[0])
        if found is not None:
            return found

    readmes = sorted(p for p in folder.rglob("*.md") if p.name.lower() == "readme.md")
    if len(readmes) == 1:
        return readmes[0]

    mds = sorted(folder.rglob("*.md"))
    if len(mds) == 1:
        return mds[0]
    if not mds:
        raise FileNotFoundError(f"No .md file found in {folder}")
    names = ", ".join(m.relative_to(folder).as_posix() for m in mds[:8])
    suffix = "..." if len(mds) > 8 else ""
    raise ValueError(f"Multiple .md files in {folder}, specify one: {names}{suffix}")


def resolve_input(token: str, cwd: Path | None = None) -> ResolvedInput:
    """Resolve a token to a markdown file.

    Accepts: a .md file, a folder containing a .md, an existing .zip (extracted
    and searched for README.md), or a .zip name with no file (folder with same stem).
    """
    cwd = cwd or Path.cwd()
    token = token.strip()
    if not token:
        raise ValueError("empty input token")

    path = Path(token)
    candidates = [path]
    if not path.is_absolute():
        candidates.insert(0, cwd / path)

    for candidate in candidates:
        if not candidate.exists():
            continue
        resolved = candidate.resolve()

        if resolved.is_file() and resolved.suffix.lower() == ".zip":
            extract_dir = Path(tempfile.mkdtemp(prefix="md_downloader_src_"))
            with zipfile.ZipFile(resolved, "r") as zf:
                zf.extractall(extract_dir)
            md_path = find_md_in_folder(extract_dir)
            log.info("Extracted %s -> %s", resolved.name, md_path)
            return ResolvedInput(
                md_path=md_path,
                source_zip=resolved,
                cleanup_dirs=[extract_dir],
            )

        if resolved.is_file() and resolved.suffix.lower() == ".md":
            return ResolvedInput(md_path=resolved)

        if resolved.is_dir():
            return ResolvedInput(md_path=find_md_in_folder(resolved))

    if path.suffix.lower() == ".zip":
        folder = path.with_suffix("")
        if not folder.is_absolute():
            folder = cwd / folder
        if folder.is_dir():
            return ResolvedInput(md_path=find_md_in_folder(folder.resolve()))

    raise FileNotFoundError(f"Could not resolve markdown from: {token!r}")


def resolve_input_paths(raw: str, cwd: Path | None = None) -> list[ResolvedInput]:
    cwd = cwd or Path.cwd()
    seen: set[Path] = set()
    inputs: list[ResolvedInput] = []
    for token in split_input_tokens(raw):
        resolved = resolve_input(token, cwd)
        key = resolved.md_path.resolve()
        if key not in seen:
            seen.add(key)
            inputs.append(resolved)
    return inputs


def default_zip_path(resolved: ResolvedInput) -> Path:
    """Zip named after the source folder or input .zip, with _1 suffix, beside that folder."""
    if resolved.source_zip is not None:
        src = resolved.source_zip.resolve()
        return src.parent / f"{src.stem}_1.zip"
    parent = resolved.md_path.parent.resolve()
    return parent.parent / f"{parent.name}_1.zip"


def zip_output_dir(out_dir: Path, zip_path: Path) -> None:
    zip_path = zip_path.resolve()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(out_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, arcname=file_path.relative_to(out_dir).as_posix())


# --------------------------------------------------------------------------
# Per-file processing
# --------------------------------------------------------------------------

def process_markdown(
    md_path: Path,
    out_dir: Path,
    *,
    workers: int,
    github_token: str | None,
    timeout: int,
    branch: str | None,
    hf_max_file_mb: int,
    dry_run: bool,
) -> int:
    md_text = md_path.read_text(encoding="utf-8")
    all_links = extract_all_links(md_text)

    items_by_url: dict[str, LinkItem] = {}
    for lk in all_links:
        if lk["url"] in items_by_url:
            continue
        if lk["kind"] is not None:
            kind, meta = apply_hf_classification(lk["url"], lk["kind"], {})
            items_by_url[lk["url"]] = LinkItem(url=lk["url"], kind=kind, meta=meta)
        else:
            kind, meta = classify_generic(lk["url"])
            items_by_url[lk["url"]] = LinkItem(url=lk["url"], kind=kind, meta=meta)
    items = list(items_by_url.values())

    if not items:
        log.info("No links found in %s", md_path)
        return 0

    counts = Counter(i.kind for i in items)
    log.info("Found %d link(s) in %s: %s", len(items), md_path, ", ".join(f"{v} {k}" for k, v in counts.items()))

    if dry_run:
        for i in items:
            print(f"  [{i.kind:9s}] {i.url}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = out_dir / "downloads"
    downloader = Downloader(downloads_dir, github_token, timeout, branch, hf_max_file_mb)

    downloadable = [i for i in items if i.kind != "skip"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
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
    log.info("Done %s: %d downloaded, %d failed.", md_path.name, ok, err)
    log.info("  Project dir        -> %s", out_dir)
    log.info("  Local markdown     -> %s", output_md)
    log.info("  Downloaded content -> %s", downloads_dir)
    log.info("  Manifest           -> %s", manifest_path)
    return 0 if err == 0 else 2


def process_one_input(
    resolved: ResolvedInput,
    *,
    output_dir: str | None,
    multi_input: bool,
    workers: int,
    github_token: str | None,
    timeout: int,
    branch: str | None,
    hf_max_file_mb: int,
    dry_run: bool,
) -> int:
    md_path = resolved.md_path
    log.info("=== %s ===", resolved.output_basename())

    zip_path: Path | None = None
    staging_dir: Path | None = None
    try:
        if dry_run:
            out_dir = Path(output_dir) if output_dir else md_path.parent
        elif output_dir:
            base = Path(output_dir)
            out_dir = base if not multi_input else base / resolved.output_basename()
        else:
            staging_dir = Path(tempfile.mkdtemp(prefix="md_downloader_"))
            out_dir = staging_dir
            zip_path = default_zip_path(resolved)

        code = process_markdown(
            md_path,
            out_dir,
            workers=workers,
            github_token=github_token,
            timeout=timeout,
            branch=branch,
            hf_max_file_mb=hf_max_file_mb,
            dry_run=dry_run,
        )

        if staging_dir is not None and zip_path is not None:
            zip_output_dir(staging_dir, zip_path)
            shutil.rmtree(staging_dir)
            staging_dir = None
            log.info("  Result zip         -> %s", zip_path)

        return code
    finally:
        for cleanup_dir in resolved.cleanup_dirs:
            if cleanup_dir.exists():
                shutil.rmtree(cleanup_dir, ignore_errors=True)
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "md_file",
        help="One or more inputs, comma-separated (quote with ' or \" to protect commas). "
             "Each item may be a .md file, a folder containing a .md, an existing .zip "
             "(extracted and searched for README.md), or a .zip name with no file "
             "(resolved to the folder with the same stem).",
    )
    parser.add_argument("output_dir", nargs="?", default=None,
                         help="Directory to write results into. If omitted, each input is "
                              "processed into a temporary folder and saved as a .zip named "
                              "after the markdown's parent folder, placed beside that folder. "
                              "Not required with --dry-run.")
    parser.add_argument("--workers", type=int, default=6,
                         help="Concurrent downloads per input file")
    parser.add_argument("--jobs", type=int, default=0,
                         help="Input files to process in parallel (default: all inputs at once)")
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

    try:
        inputs = resolve_input_paths(args.md_file)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    if not inputs:
        log.error("No markdown inputs resolved from: %s", args.md_file)
        return 1

    file_jobs = args.jobs if args.jobs > 0 else len(inputs)
    file_jobs = max(1, min(file_jobs, len(inputs)))
    multi_input = len(inputs) > 1

    if file_jobs > 1:
        log.info("Processing %d input(s) with %d parallel job(s), %d download worker(s) each",
                 len(inputs), file_jobs, args.workers)

    common = dict(
        output_dir=args.output_dir,
        multi_input=multi_input,
        workers=args.workers,
        github_token=args.github_token,
        timeout=args.timeout,
        branch=args.branch,
        hf_max_file_mb=args.hf_max_file_mb,
        dry_run=args.dry_run,
    )

    exit_code = 0
    if file_jobs == 1:
        for resolved in inputs:
            exit_code = max(exit_code, process_one_input(resolved, **common))
    else:
        with ThreadPoolExecutor(max_workers=file_jobs) as pool:
            futures = [pool.submit(process_one_input, resolved, **common) for resolved in inputs]
            for fut in as_completed(futures):
                exit_code = max(exit_code, fut.result())

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
