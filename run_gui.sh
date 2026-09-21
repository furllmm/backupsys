#!/usr/bin/env bash
# run_gui.sh
# ==========
# Convenience launcher for Linux/macOS, symmetrical with run_gui.bat on
# Windows. Runs run_gui.py using whichever Python 3 is on PATH, from this
# script's own directory (so it works regardless of your current shell
# directory). Prefers `python3` since plain `python` is not guaranteed to
# exist or point at Python 3 on every distro.
#
# Make it executable once with:  chmod +x run_gui.sh
# Then run with:                 ./run_gui.sh

set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    exec python3 run_gui.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python run_gui.py "$@"
else
    echo "error: no 'python3' or 'python' found on PATH" >&2
    exit 1
fi
