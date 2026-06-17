#!/usr/bin/env bash
# run.sh - bootstrap a venv (first run only) and execute md_downloader.py
#
# Looks for [[paper]] and [[project]] tags in your markdown file and
# downloads the URL that follows each one (paper -> .pdf, project -> .zip).
#
# Usage:
#   ./run.sh notes.md ./notes_offline
#   ./run.sh notes.md ./notes_offline --branch main --github-token $GITHUB_TOKEN
#   ./run.sh notes.md --dry-run            # output dir not needed for a dry run
#
# Arg 1: path to your markdown file
# Arg 2: path to the (new) directory to create with the local markdown
#        copy + downloads/ folder
#
# Any extra flags are forwarded as-is to md_downloader.py.

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
