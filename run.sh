#!/usr/bin/env bash
# run.sh - bootstrap a venv (first run only) and execute md_downloader.py
#
# Downloads every URL in markdown file(s) for fully offline browsing.
#
# Usage:
#   ./run.sh notes.md ./notes_offline
#   ./run.sh notes.md                              # writes <parent>_1.zip
#   ./run.sh "'a.zip', 'b/README.md', c'"         # multiple inputs in parallel
#   ./run.sh notes.md --dry-run
#   ./run.sh notes.md ./out --jobs 4 --workers 8
#
# Arg 1: one or more inputs (comma-separated, quote-aware):
#        .md file, folder with a .md, or .zip (extracted for README.md)
# Arg 2: (optional) output directory. If omitted, each input becomes a
#        <parent-folder>_1.zip beside that folder.
#
# Extra flags are forwarded to md_downloader.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQS="$SCRIPT_DIR/requirements.txt"
STAMP="$VENV_DIR/.deps_installed"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "[setup] Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

# (Re)install deps if the venv is new or requirements.txt changed since last run.
if [ ! -f "$STAMP" ] || [ "$REQS" -nt "$STAMP" ]; then
    echo "[setup] Installing dependencies ..."
    "$VENV_DIR/bin/pip" install --upgrade pip -q
    "$VENV_DIR/bin/pip" install -r "$REQS" -q
    touch "$STAMP"
fi

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/md_downloader.py" "$@"
